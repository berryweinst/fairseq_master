import logging
import numpy as np
import torch
from torch import Tensor
import types
from typing import Optional, Dict, List
import math
from fairseq import search, utils
from fairseq.sequence_generator import SequenceGenerator, EnsembleModel
from fairseq.data import data_utils, FairseqDataset, iterators
from fairseq.tasks import register_task
from fairseq.models.fairseq_encoder import EncoderOut
from fairseq.tasks.translation import TranslationTask
from .single_shot_dataset import SingleShotLanguagePairDataset

logger = logging.getLogger(__name__)


class PredkSearch(search.Search):
    def __init__(self, tgt_dict):
        super().__init__(tgt_dict)


class SingleShotSequenceGenerator(SequenceGenerator):

    def __init__(self, models, tgt_dict, **kwargs):
        super().__init__(models, tgt_dict, **kwargs)
        self.search = (PredkSearch(tgt_dict))
        self.words_start = torch.tensor(np.array([
                tgt_dict[i].endswith("@@")
                for i in range(len(tgt_dict))
            ]))
        self.tgt_dict = tgt_dict

    @torch.no_grad()
    def generate(self, models, sample, **kwargs):
        """Generate a batch of translations.

        Args:
            models (List[~fairseq.models.FairseqModel]): ensemble of models
            sample (dict): batch
            prefix_tokens (torch.LongTensor, optional): force decoder to begin
                with these tokens
            bos_token (int, optional): beginning of sentence token
                (default: self.eos)
        """
        # self.model.reset_incremental_state()
        return self._generate(sample, **kwargs)




    @torch.no_grad()
    def _generate(
        self,
        sample: Dict[str, Dict[str, Tensor]],
        prefix_tokens: Optional[Tensor] = None,
        bos_token: Optional[int] = None,
        training: Optional[bool]=True,
    ):
        net_input = sample["net_input"]
        src_tokens = net_input["src_tokens"]
        # length of the source text being the character length except EndOfSentence and pad
        src_lengths = (src_tokens.ne(self.pad)).long().sum(dim=1)
        # bsz: total number of sentences in beam
        input_size = src_tokens.size()
        # bsz, src_len = input_size[0]//2 if training else input_size[0], input_size[1]
        bsz, src_len = input_size[0], input_size[1]

        if self.match_source_len:
            max_len = src_lengths.max().item()
        else:
            max_len = min(
                int(self.max_len_a * src_len + self.max_len_b),
                # exclude the EOS marker
                self.model.max_decoder_positions() - 1,
            )

        pred_out = self.model.single_model(**sample['net_input'], training=training)
        scores, tokens = pred_out[0].topk(1, -1)
        scores, tokens = scores.squeeze(), tokens.squeeze()

        finalized = [[] for _ in range(bsz)]

        for i, sent in enumerate(tokens):
            # _, sorted_indices = torch.sort(ti[i])
            # sorted_sent = sent[sorted_indices]

            try:
                eos_pos = (sent == self.eos).nonzero()[0]
            except:
                eos_pos = max_len + 1
            sent = sent[: eos_pos + 1]

            hyp = {
                'tokens': sent,
                'score': scores[i].mean(),
                'attention': None,
                'alignment': None,
                'positional_scores': scores[i],
            }
            finalized[i].append(hyp)

        return finalized



class SingleShotEnsembleModel(EnsembleModel):
    """A wrapper around an ensemble of models."""

    def __init__(self, models):
        super().__init__(models)
        self.incremental_states = None

    @torch.no_grad()
    def forward_decoder(self, tokens, prev_positions, pred_positions, pos_k, encoder_outs, temperature=1.):
        return self._decode_one(
            tokens,
            prev_positions,
            pred_positions,
            self.models[0],
            encoder_outs[0] if self.has_encoder() else None,
            self.incremental_states,
            log_probs=True,
            temperature=temperature,
        )


    def _decode_one(
            self, tokens, prev_positions,
            pred_positions,
            model, encoder_out,
            incremental_states, log_probs,
            temperature=1.,
    ):
        pred_out = self.model.forward(tokens)


        probs = model.get_normalized_probs(pred_out, log_probs=log_probs)
        probs = probs[:, -1:, :]
        return probs






@register_task('single_shot_translation')
class SingleShotTranslationTask(TranslationTask):
    """
    Translate from one (source) language to another (target) language.

    Args:
        src_dict (~fairseq.data.Dictionary): dictionary for the source language
        tgt_dict (~fairseq.data.Dictionary): dictionary for the target language

    .. note::

        The translation task is compatible with :mod:`fairseq-train`,
        :mod:`fairseq-generate` and :mod:`fairseq-interactive`.

    The translation task provides the following additional command-line
    arguments:

    .. argparse::
        :ref: fairseq.tasks.translation_parser
        :prog:
    """

    @staticmethod
    def add_args(parser):
        super(SingleShotTranslationTask, SingleShotTranslationTask).add_args(parser)


    def __init__(self, args, src_dict, tgt_dict):
        super().__init__(args, src_dict, tgt_dict)


    def load_dataset(self, split, epoch=1, combine=False, **kwargs):
        super().load_dataset(split, epoch=1, combine=False, **kwargs)
        self.datasets[split] = SingleShotLanguagePairDataset.from_pair_dataset(
            self.datasets[split])


    def inference_step(self, generator, models, sample, prefix_tokens=None, training=True):
        with torch.no_grad():
            return generator.generate(models, sample, prefix_tokens=prefix_tokens, training=training)



    def _inference_with_bleu(self, generator, sample, model):
        import sacrebleu

        def decode(toks, escape_unk=False):
            s = self.tgt_dict.string(
                toks.int().cpu(),
                self.args.eval_bleu_remove_bpe,
                # The default unknown string in fairseq is `<unk>`, but
                # this is tokenized by sacrebleu as `< unk >`, inflating
                # BLEU scores. Instead, we use a somewhat more verbose
                # alternative that is unlikely to appear in the real
                # reference, but doesn't get split into multiple tokens.
                unk_string=(
                    "UNKNOWNTOKENINREF" if escape_unk else "UNKNOWNTOKENINHYP"
                ),
            )
            if self.tokenizer:
                s = self.tokenizer.decode(s)
            return s

        gen_out = self.inference_step(generator, [model], sample, None)
        hyps, refs = [], []
        for i in range(len(gen_out)):
            hyps.append(decode(gen_out[i][0]['tokens']))
            refs.append(decode(
                utils.strip_pad(sample['target'][i], self.tgt_dict.pad()),
                escape_unk=True,  # don't count <unk> as matches to the hypo
            ))
        if self.args.eval_bleu_print_samples:
            logger.info('example hypothesis: ' + hyps[0])
            logger.info('example reference: ' + refs[0])
        if self.args.eval_tokenized_bleu:
            return sacrebleu.corpus_bleu(hyps, [refs], tokenize='none')
        else:
            return sacrebleu.corpus_bleu(hyps, [refs])




    def build_generator(self, models, args):
        if getattr(args, "score_reference", False):
            from fairseq.sequence_scorer import SequenceScorer

            return SequenceScorer(
                self.target_dictionary,
                compute_alignment=getattr(args, "print_alignment", False),
            )

        from fairseq.sequence_generator import (
            SequenceGenerator,
            SequenceGeneratorWithAlignment,
        )

        # Choose search strategy. Defaults to Beam Search.
        sampling = getattr(args, "sampling", False)
        sampling_topk = getattr(args, "sampling_topk", -1)
        sampling_topp = getattr(args, "sampling_topp", -1.0)
        diverse_beam_groups = getattr(args, "diverse_beam_groups", -1)
        diverse_beam_strength = getattr(args, "diverse_beam_strength", 0.5)
        match_source_len = getattr(args, "match_source_len", False)
        diversity_rate = getattr(args, "diversity_rate", -1)
        if (
            sum(
                int(cond)
                for cond in [
                    sampling,
                    diverse_beam_groups > 0,
                    match_source_len,
                    diversity_rate > 0,
                ]
            )
            > 1
        ):
            raise ValueError("Provided Search parameters are mutually exclusive.")
        assert sampling_topk < 0 or sampling, "--sampling-topk requires --sampling"
        assert sampling_topp < 0 or sampling, "--sampling-topp requires --sampling"

        if sampling:
            search_strategy = search.Sampling(
                self.target_dictionary, sampling_topk, sampling_topp
            )
        elif diverse_beam_groups > 0:
            search_strategy = search.DiverseBeamSearch(
                self.target_dictionary, diverse_beam_groups, diverse_beam_strength
            )
        elif match_source_len:
            # this is useful for tagging applications where the output
            # length should match the input length, so we hardcode the
            # length constraints for simplicity
            search_strategy = search.LengthConstrainedBeamSearch(
                self.target_dictionary,
                min_len_a=1,
                min_len_b=0,
                max_len_a=1,
                max_len_b=0,
            )
        elif diversity_rate > -1:
            search_strategy = search.DiverseSiblingsSearch(
                self.target_dictionary, diversity_rate
            )
        else:
            search_strategy = PredkSearch(self.target_dictionary)

        if getattr(args, "print_alignment", False):
            seq_gen_cls = SequenceGeneratorWithAlignment
        else:
            seq_gen_cls = SingleShotSequenceGenerator

        return seq_gen_cls(
            models,
            self.target_dictionary,
            beam_size=getattr(args, "beam", 1),
            max_len_a=getattr(args, "max_len_a", 0),
            max_len_b=getattr(args, "max_len_b", 200),
            min_len=getattr(args, "min_len", 1),
            normalize_scores=(not getattr(args, "unnormalized", False)),
            len_penalty=getattr(args, "lenpen", 1),
            unk_penalty=getattr(args, "unkpen", 0),
            temperature=getattr(args, "temperature", 1.0),
            match_source_len=getattr(args, "match_source_len", False),
            no_repeat_ngram_size=getattr(args, "no_repeat_ngram_size", 0),
            search_strategy=search_strategy,
        )





