#    Copyright 2022 B. Lau, brian6091@gmail.com
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
#
#    Based on https://github.com/huggingface/diffusers/blob/v0.8.0/examples/dreambooth/train_dreambooth.py

import yaml
import hashlib
import itertools
import math
import random
import os
from pathlib import Path
from typing import Iterable, Optional
import inspect

import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from torch.utils.data import Dataset
from torchinfo import summary

from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from diffusers import AutoencoderKL, DDPMScheduler, DDIMScheduler, StableDiffusionPipeline, UNet2DConditionModel
from diffusers.optimization import get_scheduler, get_cosine_with_hard_restarts_schedule_with_warmup
from diffusers.training_utils import EMAModel, enable_full_determinism
from diffusers.utils.import_utils import is_xformers_available
from huggingface_hub import HfFolder, Repository, whoami

from PIL import Image
from torchvision import transforms
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer

from lora_diffusion import (
    inject_trainable_lora,
    save_lora_weight,
    extract_lora_ups_down,
    monkeypatch_lora,
    tune_lora_scale,
)

from src.datasets import FineTuningDataset, PromptDataset
from src.textual_inversion_templates import object_templates, style_templates
from src.args import parse_args
from src.model_utils import (
    find_modules_by_name_or_class,
    set_trainable_parameters,
    _find_children,
    _inject_trainable_lora,
    count_parameters,
    print_trainable_parameters,
)
from src.utils import image_grid, get_full_repo_name, get_gpu_memory_map


logger = get_logger(__name__)


def main(args):
    if args.debug:
        torch.set_printoptions(precision=10)
    
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    logging_dir = Path(args.output_dir, args.logging_dir)

    # TODO: CHECK FATAL ERRORS (e.g., no training, etc)
    if args.with_prior_preservation:
        if args.class_data_dir is None:
            raise ValueError("You must specify a data directory for class images.")
        if args.class_prompt is None:
            raise ValueError("You must specify prompt for class images.")

    # Currently, it's not possible to do gradient accumulation when training two models with accelerate.accumulate
    # This will be enabled soon in accelerate. For now, we don't allow gradient accumulation when training two models.
    # TODO (patil-suraj): Remove this check when gradient accumulation with two models is enabled in accelerate.
#     if args.train_text_encoder and args.gradient_accumulation_steps > 1 and accelerator.num_processes > 1:
#         raise ValueError(
#             "Gradient accumulation is not supported when training the text encoder in distributed training. "
#             "Please set gradient_accumulation_steps to 1. This feature will be supported in the future."
#         )
        
    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with="tensorboard",
        logging_dir=logging_dir,
    )
    
    # TODO: check instance and class (if given) path existence
    # Check https://huggingface.co/docs/accelerate/main/en/package_reference/accelerator#only-ever-once-across-all-servers
    if args.output_dir is not None:
        os.makedirs(args.output_dir, exist_ok=True)
    
    # TODO: remove config file line, and convert sets back to lists
    with open(os.path.join(args.output_dir, "args.yaml"), "w") as f:
        yaml.dump(args.__dict__, f, indent=2, sort_keys=False)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.push_to_hub:
            if args.hub_model_id is None:
                repo_name = get_full_repo_name(Path(args.output_dir).name, token=args.hub_token)
            else:
                repo_name = args.hub_model_id
            repo = Repository(args.output_dir, clone_from=repo_name)

            with open(os.path.join(args.output_dir, ".gitignore"), "w+") as gitignore:
                if "step_*" not in gitignore:
                    gitignore.write("step_*\n")
                if "epoch_*" not in gitignore:
                    gitignore.write("epoch_*\n")

    if args.seed is not None:
        if args.enable_full_determinism:
            enable_full_determinism(args.seed)
        else:
            set_seed(args.seed)

    if args.with_prior_preservation:
        pipeline = None
        class_images_dir = Path(args.class_data_dir)
        if not class_images_dir.exists():
            class_images_dir.mkdir(parents=True)
        cur_class_images = len(list(class_images_dir.iterdir()))

        # Generate class images if necessary
        if cur_class_images < args.num_class_images:
            torch_dtype = torch.float16 if accelerator.device.type == "cuda" else torch.float32
            if pipeline is None:
                pipeline = StableDiffusionPipeline.from_pretrained(
                    args.pretrained_model_name_or_path,
                    vae=AutoencoderKL.from_pretrained(
                        args.pretrained_vae_name_or_path or args.pretrained_model_name_or_path,
                        subfolder=None if args.pretrained_vae_name_or_path else "vae",
                        revision=None if args.pretrained_vae_name_or_path else args.revision,
                        torch_dtype=torch_dtype,
                    ),
                    torch_dtype=torch_dtype,
                    safety_checker=None,
                    revision=args.revision
                )
                pipeline.set_progress_bar_config(disable=True)
                pipeline.to(accelerator.device)

            num_new_images = args.num_class_images - cur_class_images
            logger.info(f"Number of class images to sample: {num_new_images}.")

            sample_dataset = PromptDataset(args.class_prompt, num_new_images)
            sample_dataloader = torch.utils.data.DataLoader(sample_dataset, batch_size=args.sample_batch_size)

            sample_dataloader = accelerator.prepare(sample_dataloader)

            for example in tqdm(
                sample_dataloader, desc="Generating class images", disable=not accelerator.is_local_main_process
            ):
                images = pipeline(example["prompt"]).images

                for i, image in enumerate(images):
                    hash_image = hashlib.sha1(image.tobytes()).hexdigest()
                    image_filename = class_images_dir / f"{example['index'][i] + cur_class_images}-{hash_image}.jpg"
                    image.save(image_filename)

            del pipeline
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    # Load diffusion components
    if args.pretrained_tokenizer_name_or_path is not None:
        tokenizer = CLIPTokenizer.from_pretrained(
            args.pretrained_tokenizer_name_or_path,
        )
    elif args.pretrained_model_name_or_path:
        tokenizer = CLIPTokenizer.from_pretrained(
            args.pretrained_model_name_or_path,
            subfolder="tokenizer",
            revision=args.revision,
        )

    text_encoder = CLIPTextModel.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=args.revision,
    )
    
    if args.add_instance_token:
        # TODO: move to model_utils
        num_added_tokens = tokenizer.add_tokens(args.instance_token)
        if num_added_tokens == 0:
            raise ValueError(
                f"The tokenizer already contains the token {args.instance_token}. Please pass a different"
                " `instance_token` that is not already in the tokenizer."
            )
        else:
            if args.debug:
                print(f"{args.instance_token} added to tokenizer.")

        # Resize the token embeddings
        text_encoder.resize_token_embeddings(len(tokenizer))
        
        if args.class_token is not None:
            # Convert the class_token to ids
            token_ids = tokenizer.encode(args.class_token, add_special_tokens=False)
            class_token_id = token_ids[0]
            if len(token_ids) > 1:
                raise ValueError("The class token must be a single token.")

            # Initialise new instance_token embedding with the embedding of the class_token
            token_embeds = text_encoder.get_input_embeddings().weight.data
            instance_token_id = tokenizer.convert_tokens_to_ids(args.instance_token)
            if args.debug:
                print("Instance weights: ")
                print(token_embeds[instance_token_id])
            token_embeds[instance_token_id] = token_embeds[class_token_id]
            if args.debug:
                print("Instance weights intialized: ")
                print(token_embeds[instance_token_id])

    vae = AutoencoderKL.from_pretrained(        
        args.pretrained_vae_name_or_path or args.pretrained_model_name_or_path,
        subfolder=None if args.pretrained_vae_name_or_path else "vae",
        revision=None if args.pretrained_vae_name_or_path else args.revision,
    )
    
    unet = UNet2DConditionModel.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="unet",
        revision=args.revision,
    )

    if is_xformers_available():
        try:
            unet.enable_xformers_memory_efficient_attention()
        except Exception as e:
            logger.warning(
                "Could not enable memory efficient attention. Make sure xformers is installed"
                f" correctly and a GPU is available: {e}"
            )
    
    vae.requires_grad_(False)
    
    unet.requires_grad_(False)
    set_trainable_parameters(unet,
                             target_module_or_class=args.train_unet_module_or_class,
                             target_submodule=args.train_unet_submodule,
                             lora_layer=args.lora_unet_layer,
                             lora_rank=args.lora_unet_rank,
                             lora_train_off_target=args.lora_unet_train_off_target)
        
    text_encoder.requires_grad_(False)
    set_trainable_parameters(text_encoder,
                             target_module_or_class=args.train_text_module_or_class,
                             target_submodule=args.train_text_submodule,
                             lora_layer=args.lora_text_layer,
                             lora_rank=args.lora_text_rank,
                             lora_train_off_target=args.lora_text_train_off_target)

    if True:#args.debug: # TODO: accept file handles and add save_parameter_summary
        #print_trainable_parameters(unet)
        count_parameters(unet)
        #print_trainable_parameters(text_encoder)
        count_parameters(text_encoder)
    
    if args.debug:
        print(summary(unet, col_names=["num_params", "trainable"], verbose=1))
        print(summary(text_encoder, col_names=["num_params", "trainable"], verbose=1))

        with open(os.path.join(args.output_dir, "unet_layout.txt"), "w") as f:
            f.write(str(summary(unet, col_names=["num_params", "trainable"], verbose=2)))
            f.close()
        with open(os.path.join(args.output_dir, "text_encoder_layout.txt"), "w") as f:
            f.write(str(summary(text_encoder, col_names=["num_params", "trainable"], verbose=2)))
            f.close()
    
    if args.lr_scale:
        args.learning_rate = (
            args.learning_rate * args.gradient_accumulation_steps * args.train_batch_size * accelerator.num_processes
        )
        
    args.learning_rate_text = (
        args.learning_rate
        if args.learning_rate_text is None
        else args.learning_rate_text
    )
    
    unet_params_to_optimize = {
        "params": [p for p in unet.parameters() if p.requires_grad],
        "lr": args.learning_rate,
    }
    train_unet = len(unet_params_to_optimize["params"])>0
    
    text_params_to_optimize = {
        "params": [p for p in text_encoder.parameters() if p.requires_grad],
        "lr": args.learning_rate_text,
    }
    train_text_encoder = len(text_params_to_optimize["params"])>0
    
    params_to_optimize = []
    if train_unet:
        params_to_optimize.append(unet_params_to_optimize)
    if train_text_encoder:
        params_to_optimize.append(text_params_to_optimize)    
        
    if len(params_to_optimize)==0:
        raise ValueError("This configuration does not train anything.")
            
    if args.gradient_checkpointing:
        if train_unet:
            unet.enable_gradient_checkpointing()
        if train_text_encoder:
            text_encoder.gradient_checkpointing_enable()
    
    # Use 8-bit Adam for lower memory usage or to fine-tune the model in 16GB GPUs
    if args.optimizer=="AdamW8bit":
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "To use 8-bit Adam, please install the bitsandbytes library: `pip install bitsandbytes`."
            )

        optimizer_class = bnb.optim.AdamW8bit
    elif args.optimizer=="AdamW":
        optimizer_class = torch.optim.AdamW
    else:
        raise ValueError(
            f"Optimizer {args.optimizer} not supported yet."
        )        

    optimizer = optimizer_class(
        params_to_optimize,
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    noise_scheduler = DDPMScheduler.from_pretrained(args.pretrained_model_name_or_path, subfolder="scheduler")

    # TODO: move to dataloader class
    if args.prompt_templates==None:
        prompt_templates = None
    elif args.prompt_templates=="object":
        prompt_templates = object_templates
    elif args.prompt_templates=="style":
        prompt_templates = style_templates
    else:
        raise ValueError(
            f"{args.prompt_templates} is not a known set of prompt templates."
        )
        
    train_dataset = FineTuningDataset(
        tokenizer=tokenizer,
        add_instance_token=args.add_instance_token,
        instance_data_root=args.instance_data_dir,
        instance_token=args.instance_token,
        instance_prompt=args.instance_prompt,
        prompt_templates=prompt_templates,
        class_data_root=args.class_data_dir if args.with_prior_preservation else None,
        class_prompt=args.class_prompt,
        use_image_captions=args.use_image_captions,
        unconditional_prompt=" ",
        size=args.resolution,
        augment_output_dir=None if args.augment_output_dir=="" else args.augment_output_dir,
        augment_min_resolution=args.augment_min_resolution,
        augment_center_crop=args.augment_center_crop,
        augment_hflip=args.augment_hflip,
        debug=args.debug,
    )
    
    # TODO: make non-nested?
    def collate_fn(examples):
        input_ids = [example["instance_prompt_ids"] for example in examples]
        pixel_values = [example["instance_images"] for example in examples]

        # Concat class and instance examples for prior preservation.
        # We do this to avoid doing two forward passes.
        if args.with_prior_preservation:
            input_ids += [example["class_prompt_ids"] for example in examples]
            pixel_values += [example["class_images"] for example in examples]
        
        # Apply text-conditioning dropout by inserting uninformative prompt
        if args.conditioning_dropout_prob > 0:
            unconditional_ids = [example["unconditional_prompt_ids"] for example in examples]*2
            for i, input_id in enumerate(input_ids):
                if random.uniform(0.0, 1.0) <= args.conditioning_dropout_prob:
                    input_ids[i] = unconditional_ids[i]

        pixel_values = torch.stack(pixel_values)
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()

        input_ids = tokenizer.pad(
            {"input_ids": input_ids},
            padding="max_length",
            max_length=tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids
        
        if args.debug:
            print("in collate_fn")
            print(input_ids)

        batch = {
            "input_ids": input_ids,
            "pixel_values": pixel_values,
        }
        return batch

    train_dataloader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.train_batch_size, shuffle=True, collate_fn=collate_fn, num_workers=1
    )

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    if args.lr_scheduler=="cosine_with_restarts":
        lr_scheduler = get_cosine_with_hard_restarts_schedule_with_warmup(
            optimizer=optimizer,
            num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
            num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
            num_cycles=args.lr_cosine_num_cycles,
        )        
    else:
        lr_scheduler = get_scheduler(
            args.lr_scheduler,
            optimizer=optimizer,
            num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
            num_training_steps=args.max_train_steps * args.gradient_accumulation_steps,
        )

    if train_unet and train_text_encoder:
        unet, text_encoder, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, text_encoder, optimizer, train_dataloader, lr_scheduler
        )
    elif train_unet:
        unet, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            unet, optimizer, train_dataloader, lr_scheduler
        )
    else:
        text_encoder, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
            text_encoder, optimizer, train_dataloader, lr_scheduler
        )
        
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # Move text_encode and vae to gpu.
    # For mixed precision training we cast the vae weights to half-precision as it 
    # is only used for inference, keeping weights in full precision is not required.
    vae.to(accelerator.device, dtype=weight_dtype)
    vae.eval()
    unet.to(accelerator.device, dtype=weight_dtype)
    if not train_unet:
        unet.eval()
#    text_encoder.to(accelerator.device, dtype=weight_dtype)
#     if not train_text_encoder:
#         text_encoder.eval()

    print(f"**************************** DTYPE {weight_dtype}")

    # Create EMA for the unet.
    if args.use_ema and train_unet:
        ema_unet = EMAModel(
            accelerator.unwrap_model(unet), 
            inv_gamma=args.ema_inv_gamma, 
            power=args.ema_power, 
            min_value=args.ema_min_value,
            max_value=args.ema_max_value
        )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    # Initialize the trackers (automatically on the main process)
    if accelerator.is_main_process:
        accelerator.init_trackers("dreambooth")

    print("***** Running training *****")
    print(f"  Num examples = {len(train_dataset)}")
    print(f"  Num batches each epoch = {len(train_dataloader)}")
    print(f"  Num Epochs = {args.num_train_epochs}")
    print(f"  Instantaneous batch size per device = {args.train_batch_size}")
    print(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    print(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    print(f"  Total optimization steps = {args.max_train_steps}")

    # TODO: make this non-nested?
    def save_weights(step):
        # Create the pipeline using using the trained modules and save it.
        if accelerator.is_main_process:
            save_dir = os.path.join(args.output_dir, f"{step}")
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)

            # https://github.com/huggingface/diffusers/issues/1566
            accepts_keep_fp32_wrapper = "keep_fp32_wrapper" in set(
                inspect.signature(accelerator.unwrap_model).parameters.keys()
            )
            extra_args = (
                {"keep_fp32_wrapper": True} if accepts_keep_fp32_wrapper else {}
            )
                    
            if train_text_encoder:
                text_enc_model = accelerator.unwrap_model(text_encoder, **extra_args)
            else:
                text_enc_model = CLIPTextModel.from_pretrained(
                    args.pretrained_model_name_or_path,
                    subfolder="text_encoder",
                    )

            pipeline = StableDiffusionPipeline.from_pretrained(
                args.pretrained_model_name_or_path,
                tokenizer=tokenizer,
                unet=accelerator.unwrap_model(
                        ema_unet.averaged_model if args.use_ema else unet,
                        **extra_args,
                    ),
                text_encoder=text_enc_model,
                vae=AutoencoderKL.from_pretrained(
                    args.pretrained_vae_name_or_path or args.pretrained_model_name_or_path,
                    subfolder=None if args.pretrained_vae_name_or_path else "vae",
                    revision=None if args.pretrained_vae_name_or_path else args.revision,
                ),
                safety_checker=None,
                torch_dtype=torch.float16,
                revision=args.revision,
            )
            
            # TODO: for custom diffusion, or generally distinct module training
            
            if args.lora_unet_layer!=None or args.lora_unet_layer!=None:
                # TODO: if add_instance_token, I assume we have to save the tokenizer?
#                 save_lora_weight(
#                     pipeline.unet,
#                     os.path.join(save_dir, "lora_unet.pt"),
#                     target_replace_module=args.lora_unet_modules,
#                 )

#                 if args.train_text_encoder or args.train_text_embedding_only:
#                     save_lora_weight(
#                         pipeline.text_encoder,
#                         os.path.join(save_dir, "lora_text_encoder.pt"),
#                         target_replace_module=args.lora_text_modules,
#                     )

                # already monkeypatched, but could change alpha? TODO: add save_lora_alpha
                tune_lora_scale(pipeline.unet, 1.00)
                tune_lora_scale(pipeline.text_encoder, 1.00)
            else:
                pipeline.save_pretrained(save_dir)

            if args.save_n_sample>0:
                save_sample_prompt = args.save_sample_prompt.replace("{}", args.instance_token)
                save_sample_prompt = list(map(str.strip, save_sample_prompt.split('//')))
                pipeline = pipeline.to(accelerator.device)
                g_cuda = torch.Generator(device=accelerator.device).manual_seed(
                    args.save_seed if args.save_seed!=None else args.seed,
                )
                pipeline.set_progress_bar_config(disable=True)
                sample_dir = os.path.join(save_dir, "samples")
                os.makedirs(sample_dir, exist_ok=True)
                
                with torch.autocast("cuda"), torch.inference_mode():
                    all_images = []
                    for i in tqdm(range(args.save_n_sample), desc="Generating samples"):
                        images = pipeline(
                            save_sample_prompt,
                            negative_prompt=[args.save_sample_negative_prompt]*len(save_sample_prompt),
                            guidance_scale=args.save_guidance_scale,
                            num_inference_steps=args.save_infer_steps,
                            generator=g_cuda
                        ).images
                        all_images.extend(images)
                        
                    grid = image_grid(all_images, rows=args.save_n_sample, cols=len(save_sample_prompt))
                    grid.save(os.path.join(sample_dir, f"{step}.jpg"), quality=90, optimize=True)
                    
                del pipeline
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            print(f"[*] Weights saved at {save_dir}")

    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    progress_bar.set_description("Steps")
    global_step = 0

    # TODO: eventually move to debug
    if train_text_encoder:
        # keep original embeddings as reference
        orig_embeds_params = text_encoder.get_input_embeddings().weight.data.clone()
        if args.debug and (len(text_params_to_optimize["params"])>0) and args.add_instance_token:
            print(instance_token_id)

    for epoch in range(args.num_train_epochs):
        unet.train()
        text_encoder.train()
#         if train_unet:
#             unet.train()
#         if train_text_encoder:
#             text_encoder.train()
        for step, batch in enumerate(train_dataloader):
            # TODO: how to handle context setting when unet is not training?
            with accelerator.accumulate(text_encoder):
                # Convert images to latent space
                latents = vae.encode(batch["pixel_values"].to(dtype=weight_dtype)).latent_dist.sample()
                latents = latents * 0.18215

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]
                
                # Sample a random timestep for each image
                timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
                timesteps = timesteps.long()

                # Add noise to the latents according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

                # Get the text embedding for conditioning
                encoder_hidden_states = text_encoder(batch["input_ids"])[0]

                # Predict the noise residual
                model_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample

                # Get the target for loss depending on the prediction type
                if noise_scheduler.config.prediction_type == "epsilon":
                    target = noise
                elif noise_scheduler.config.prediction_type == "v_prediction":
                    target = noise_scheduler.get_velocity(latents, noise, timesteps)
                else:
                    raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

                if args.with_prior_preservation:
                    # Chunk the noise and model_pred into two parts and compute the loss on each part separately.
                    model_pred, model_pred_prior = torch.chunk(model_pred, 2, dim=0)
                    target, target_prior = torch.chunk(target, 2, dim=0)

                    # Compute instance loss
                    pred_loss = F.mse_loss(model_pred.float(), target.float(), reduction="none").mean([1, 2, 3]).mean()

                    # Compute prior loss
                    prior_loss = F.mse_loss(model_pred_prior.float(), target_prior.float(), reduction="mean")

                    # Add the prior loss to the instance loss.
                    loss = pred_loss + args.prior_loss_weight * prior_loss
                else:
                    loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")

                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    # TODO: this should accept params_to_optimize as first input, no?
                    params_to_clip = (
                        itertools.chain(unet.parameters(), text_encoder.parameters())
                        if train_text_encoder
                        else unet.parameters()
                    )
                    #params_to_clip = params_to_optimize
#                     accelerator.clip_grad_norm_(params_to_clip, args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                if args.use_ema:
                    ema_unet.step(unet)
                optimizer.zero_grad()

                if args.debug and train_text_encoder and args.add_instance_token:
                    # TODO: eventually move all of this to debug
                    # Let's make sure we don't update any embedding weights besides the newly added token
                    index_no_updates = torch.arange(len(tokenizer)) != instance_token_id
                    with torch.no_grad():
                        if args.debug:
                            print("Are we changing?")
                            print("original")
                            print(orig_embeds_params[index_no_updates])
                            print("After step")
                            print(text_encoder.get_input_embeddings().weight[index_no_updates])
                        text_encoder.get_input_embeddings().weight[index_no_updates] = orig_embeds_params[index_no_updates]
                
            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                
                if global_step > 0 and not global_step % args.save_interval and global_step >= args.save_min_steps:
                    save_weights(global_step)
                            
            if args.with_prior_preservation:
                logs = {"Loss/pred": pred_loss.detach().item(),
                        "Loss/prior": prior_loss.detach().item(),
                        "Loss/total": loss.detach().item(),
                       }
            else:
                logs = {"Loss/pred": loss.detach().item()}

            if train_unet and train_text_encoder:
                logs["lr/unet"] = lr_scheduler.get_last_lr()[0]
                logs["lr/text"] = lr_scheduler.get_last_lr()[1]
            else:
                logs["lr"] = lr_scheduler.get_last_lr()[0]
                
            if args.log_gpu:
                logs["GPU"] = get_gpu_memory_map()[0]
                                
            if args.use_ema:
                logs["ema_decay"] = ema_unet.decay
                    
            progress_bar.set_postfix(**logs)
            accelerator.log(logs, step=global_step)

            if global_step >= args.max_train_steps:
                break
            
        accelerator.wait_for_everyone()

    if accelerator.is_main_process:
        save_weights(global_step)
    
    accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
