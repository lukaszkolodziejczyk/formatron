import re
import typing

import kbnf
import torch
from transformers import LogitsProcessor, PreTrainedTokenizerBase, LogitsProcessorList

from config import EngineGenerationConfig
from formatter import Formatter, FormatterBuilder


def _multiple_replace(replacements, text):
    # Create a regular expression from the dictionary keys
    regex = re.compile("(%s)" % "|".join(map(re.escape, replacements.keys())))
    # For each match, look-up corresponding value in dictionary
    return regex.sub(lambda mo: replacements[mo.group()], text)


def _get_original_whitespace_characters(tokenizer, vocab, chars) -> typing.Dict[str, int]:
    old_char_to_new_char = {}
    id2tokens = {v: k for k, v in vocab.items()}
    for char in chars:
        char_ids = tokenizer.encode(char)
        if len(char_ids) != 1:
            continue
        char_id = char_ids[0]
        char_token = id2tokens[char_id]
        if char_token != char:
            old_char_to_new_char[char_token] = char
    new_vocab = {}
    for k in vocab:
        new_k = _multiple_replace(old_char_to_new_char, k)
        new_vocab[new_k] = vocab[k]
    return new_vocab


def create_engine_vocabulary(tokenizer: PreTrainedTokenizerBase) -> kbnf.Vocabulary:
    """
    Create a vocabulary for the KBNF engine.
    :param tokenizer: The tokenizer.
    :return: The vocabulary.
    """
    vocab = tokenizer.get_vocab()
    new_vocab = _get_original_whitespace_characters(tokenizer, vocab, [" ", "\n", "\t", '\n\n'])
    return kbnf.Vocabulary({v: kbnf.Token(k.encode("utf-8")) for k, v in new_vocab.items()},
                           {v: k for k, v in new_vocab.items()})


def create_formatter_logits_processor(tokenizer: PreTrainedTokenizerBase,
                                      formatter_builders: typing.Sequence[FormatterBuilder] | FormatterBuilder,
                                      configs: typing.Sequence[EngineGenerationConfig] = None) -> LogitsProcessor:
    """
    Create a formatter logits processor.
    """
    vocab = create_engine_vocabulary(tokenizer)
    if not isinstance(formatter_builders, typing.Sequence):
        formatter_builders = [formatter_builders]
    formatters = [i.build(vocab, lambda tokens: tokenizer.decode(tokens)) for i in formatter_builders]
    return FormattersLogitsProcessor(formatters, tokenizer.eos_token_id, configs)


def create_formatter_logits_processor_list(tokenizer: PreTrainedTokenizerBase,
                                           formatter_builders: typing.Sequence[FormatterBuilder] | FormatterBuilder,
                                           configs: typing.Sequence[EngineGenerationConfig] = None) \
        -> LogitsProcessorList:
    """
    Create a formatter logits processor list.
    """
    return LogitsProcessorList([create_formatter_logits_processor(tokenizer, formatter_builders, configs)])


class FormattersLogitsProcessor(LogitsProcessor):
    """
    Logit processor that uses formatters to mask batch logits.
    """

    def __init__(self, formatters: typing.Sequence[Formatter], eos_token_id: int,
                 configs: typing.Sequence[EngineGenerationConfig] = None):
        self._formatters = formatters
        self._eos_token_id = eos_token_id
        self._last_input_id_length = None
        if configs is None:
            configs = [EngineGenerationConfig() for _ in formatters]
        assert len(configs) == len(formatters), \
            f"Number of formatters({len(formatters)}) must match number of configs({len(configs)})"
        self.configs = configs

    def __call__(self, input_ids, scores):
        assert input_ids.shape[0] == len(self._formatters), (f"Number of formatters({len(self._formatters)})"
                                                             f" must match batch size({input_ids.shape[0]})")
        if self._last_input_id_length is None:  # First iteration
            self._last_input_id_length = input_ids.shape[1]
            for formatter, config, prompt in zip(self._formatters, self.configs, input_ids):
                if config.reset_on_completion and formatter.is_completed():
                    formatter.reset()
                if config.read_prompt:
                    for token in prompt:
                        formatter.accept_token(token)
        else:
            assert input_ids.shape[1] == self._last_input_id_length + 1, ("One iteration in generation loop"
                                                                          " must add exactly one token.")
            self._last_input_id_length += 1
            for formatter, input_id in zip(self._formatters, input_ids[:, -1]):
                if input_id != self._eos_token_id:
                    formatter.accept_token(input_id)
        for i, formatter in enumerate(self._formatters):
            if formatter.is_completed():
                scores[i, :] = float("-inf")
                scores[i, self._eos_token_id] = 0.0
                continue
            formatter.compute_allowed_tokens()
            score = scores[i, :]
            new_score = formatter.mask_logits(score)

            if score is not new_score:  # Avoid unnecessary copy
                scores[i, :] = new_score
        return scores