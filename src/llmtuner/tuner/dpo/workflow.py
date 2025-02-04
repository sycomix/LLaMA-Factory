# Inspired by: https://github.com/huggingface/trl/blob/main/examples/research_projects/stack_llama_2/scripts/dpo_llama2.py

from peft import PeftModel
from typing import TYPE_CHECKING, Optional, List
from transformers import Seq2SeqTrainingArguments

from llmtuner.dsets import get_dataset, preprocess_dataset, split_dataset
from llmtuner.extras.constants import IGNORE_INDEX
from llmtuner.extras.logging import get_logger
from llmtuner.extras.ploting import plot_loss
from llmtuner.hparams import ModelArguments
from llmtuner.tuner.core import generate_model_card, load_model_and_tokenizer
from llmtuner.tuner.dpo.collator import DPODataCollatorWithPadding
from llmtuner.tuner.dpo.trainer import CustomDPOTrainer

if TYPE_CHECKING:
    from transformers import TrainerCallback
    from llmtuner.hparams import DataArguments, FinetuningArguments


logger = get_logger(__name__)


def run_dpo(
    model_args: "ModelArguments",
    data_args: "DataArguments",
    training_args: "Seq2SeqTrainingArguments",
    finetuning_args: "FinetuningArguments",
    callbacks: Optional[List["TrainerCallback"]] = None
):
    dataset = get_dataset(model_args, data_args)
    model, tokenizer = load_model_and_tokenizer(model_args, finetuning_args, training_args.do_train, stage="sft")
    dataset = preprocess_dataset(dataset, tokenizer, data_args, training_args, stage="rm")
    data_collator = DPODataCollatorWithPadding(
        tokenizer=tokenizer,
        pad_to_multiple_of=4,
        label_pad_token_id=IGNORE_INDEX if data_args.ignore_pad_token_for_loss else tokenizer.pad_token_id
    )

    # Create reference model
    if finetuning_args.dpo_ref_model is not None:
        ref_model_args_dict = model_args.to_dict()
        ref_model_args_dict.update(dict(
            model_name_or_path=finetuning_args.dpo_ref_model,
            checkpoint_dir=finetuning_args.dpo_ref_model_checkpoint
        ))
        ref_model_args = ModelArguments(**ref_model_args_dict)
        ref_model, _ = load_model_and_tokenizer(ref_model_args, finetuning_args, is_trainable=False, stage="sft")
        logger.info(f"Created reference model from {finetuning_args.dpo_ref_model}")
    elif training_args.do_train:
        if isinstance(model, PeftModel):
            ref_model = None
        else:
            ref_model, _ = load_model_and_tokenizer(model_args, finetuning_args, is_trainable=False, stage="sft")
            logger.info("Created reference model from the model itself.")
    else:
        ref_model = model

    # Update arguments
    training_args_dict = training_args.to_dict()
    training_args_dict.update(dict(remove_unused_columns=False)) # important for pairwise dataset
    training_args = Seq2SeqTrainingArguments(**training_args_dict)

    # Initialize our Trainer
    trainer = CustomDPOTrainer(
        beta=finetuning_args.dpo_beta,
        model=model,
        ref_model=ref_model,
        args=training_args,
        tokenizer=tokenizer,
        data_collator=data_collator,
        callbacks=callbacks,
        **split_dataset(dataset, data_args, training_args)
    )

    # Training
    if training_args.do_train:
        train_result = trainer.train(resume_from_checkpoint=training_args.resume_from_checkpoint)
        trainer.save_model()
        trainer.log_metrics("train", train_result.metrics)
        trainer.save_metrics("train", train_result.metrics)
        trainer.save_state()
        if trainer.is_world_process_zero() and model_args.plot_loss:
            plot_loss(training_args.output_dir, keys=["loss", "eval_loss"])

    # Evaluation
    if training_args.do_eval:
        metrics = trainer.evaluate(metric_key_prefix="eval")
        if id(model) == id(ref_model): # unable to compute rewards without a reference model
            logger.warning("Pass `dpo_ref_model` for computing rewards at evaluation.")
            remove_keys = [key for key in metrics.keys() if "rewards" in key]
            for key in remove_keys:
                metrics.pop(key)
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    # Create model card
    if training_args.do_train:
        if training_args.push_to_hub:
            trainer.push_to_hub(**generate_model_card(model_args, data_args, finetuning_args))
        else:
            trainer.create_model_card(**generate_model_card(model_args, data_args, finetuning_args))
