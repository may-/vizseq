# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from typing import Tuple, List, Iterable, NamedTuple, Dict, Optional

import numpy as np

from vizseq._data import VizSeqDataSources, VizSeqLanguageTagger
from .data_sorters import (VizSeqSortingType, VizSeqRandomSorter,
                           VizSeqByLenSorter, VizSeqByStrOrderSorter,
                           VizSeqByMetricSorter, VizSeqByMetricDiffSorter)
from .data_filter import VizSeqFilter
from vizseq.scorers import get_scorer, get_scorer_ids
from vizseq._visualizers import (VizSeqSrcVisualizer, VizSeqRefVisualizer,
                                 VizSeqHypoVisualizer, VizSeqDictVisualizer)
from vizseq._utils.logger import logger

DEFAULT_PAGE_SIZE = 10
DEFAULT_PAGE_NO = 1
MAX_PAGE_SZ = 100


def _get_start_end_idx(
        n_items: int, page_sz: int, page_no: int
) -> Tuple[int, int]:
    assert page_sz > 0 and page_no > 0
    start_idx = min(page_sz * (page_no - 1), n_items - 1)
    end_idx = max(page_sz * page_no - 1, start_idx)
    # inclusive on both sides
    return start_idx, end_idx


def _select(a_list: List, indices: List[int]) -> List:
    return [a_list[i] for i in indices if i < len(a_list)]


class VizSeqPageData(NamedTuple):
    viz_src: List[List[str]]
    viz_ref: List[List[str]]
    viz_hypo: Dict[str, List[str]]
    cur_src_text: List[str]
    cur_src: List[List[str]]
    cur_ref: List[List[str]]
    cur_idx: List[int]
    cur_tags: List[int]
    viz_sent_scores: List[Dict[str, Dict[str, float]]]
    trg_lang: Optional[List[str]]
    n_cur_samples: int
    n_samples: int
    total_examples: int


class VizSeqDataPageView(object):
    @classmethod
    def get_enum(cls, data: Optional[Iterable]) -> List:
        if data is None:
            return []
        return [
            [i] + list(e) if isinstance(e, (list, tuple)) else [i, e]
            for i, e in enumerate(data)
        ]

    @classmethod
    def get(
            cls, src: VizSeqDataSources, ref: VizSeqDataSources,
            hypo: VizSeqDataSources, page_sz: int, page_no: int,
            metrics: Optional[List[str]] = None, query: str = '',
            sorting: int = 0, sorting_metric: str = '',
            need_lang_tags: bool = False, disable_alignment: bool = False,
            tags: Optional[List[str]] = [],
            is_tag_selected: Optional[List[str]] = [],
            filter_by_tags: bool = False,
    ) -> VizSeqPageData:
        assert page_no > 0 and page_sz > 0
        page_sz = min(page_sz, MAX_PAGE_SZ)
        metrics = [] if metrics is None else metrics
        models = hypo.text_names

        # query
        cur_idx = list(range(len(src)))
        if src.has_text:
            cur_idx = VizSeqFilter.filter(src.text, query)
        elif ref.has_text:
            cur_idx = VizSeqFilter.filter(ref.text, query)

        # filter by tags
        if filter_by_tags:
            cur_idx = [idx for idx, flag in zip(cur_idx, is_tag_selected) if flag]

        n_samples = len(cur_idx)

        # sorting
        sorting = {e.value: e for e in VizSeqSortingType}.get(sorting, None)
        assert sorting is not None
        if sorting == VizSeqSortingType.random:
            cur_idx = VizSeqRandomSorter.sort(cur_idx)
        elif sorting == VizSeqSortingType.ref_len:
            cur_idx = VizSeqByLenSorter.sort(ref.main_text, cur_idx)
        elif sorting == VizSeqSortingType.ref_alphabetical:
            cur_idx = VizSeqByStrOrderSorter.sort(ref.main_text, cur_idx)
        elif sorting == VizSeqSortingType.src_len:
            if src.has_text:
                cur_idx = VizSeqByLenSorter.sort(src.main_text, cur_idx)
        elif sorting == VizSeqSortingType.src_alphabetical:
            if src.has_text:
                cur_idx = VizSeqByStrOrderSorter.sort(src.main_text, cur_idx)
        elif sorting in [VizSeqSortingType.metric, VizSeqSortingType.metric_diff]:
            if sorting_metric in get_scorer_ids():
                _cur_ref = [_select(t, cur_idx) for t in ref.text]
                scores = {
                    m: get_scorer(sorting_metric)(
                        corpus_level=False, sent_level=True
                    ).score(_select(t, cur_idx), _cur_ref).sent_scores
                    for m, t in zip(models, hypo.text)
                }
                scores = [
                    {m: scores[m][i] for m in models}
                    for i in range(len(cur_idx))
                ]
                if sorting == VizSeqSortingType.metric:
                    cur_idx = VizSeqByMetricSorter.sort(scores, cur_idx)
                elif sorting == VizSeqSortingType.metric_diff:
                    cur_idx = VizSeqByMetricDiffSorter.sort(scores, cur_idx)

        # pagination
        start_idx, end_idx = _get_start_end_idx(len(cur_idx), page_sz, page_no)
        cur_idx = cur_idx[start_idx: end_idx + 1]
        n_cur_samples = len(cur_idx)

        # page data
        cur_src = src.cached(cur_idx)
        cur_src_text = _select(src.main_text, cur_idx) if src.has_text else None
        cur_ref = [_select(t, cur_idx) for t in ref.text]
        cur_hypo = {
            n: _select(t, cur_idx) for n, t in zip(models, hypo.text)
        }

        # sent scores
        cur_sent_scores = {
            s: {
                m: np.round(get_scorer(s)(
                    corpus_level=False, sent_level=True
                ).score(hh, cur_ref).sent_scores, decimals=2)
                for m, hh in cur_hypo.items()
            }
            for s in metrics
        }

        # rendering
        viz_src = cur_src
        if not disable_alignment:
            viz_src = VizSeqSrcVisualizer.visualize(cur_src, src.text_indices)
        viz_ref = cur_ref
        if not disable_alignment and cur_src_text is not None:
            viz_ref = VizSeqRefVisualizer.visualize(
                cur_src_text, cur_ref, src.main_text_idx
            )
        viz_hypo = cur_hypo
        if not disable_alignment:
            viz_hypo = VizSeqHypoVisualizer.visualize(cur_ref[0], cur_hypo, 0)
        viz_sent_scores = [
            {
                s: VizSeqDictVisualizer.visualize(
                    {m: cur_sent_scores[s][m][i] for m in models}
                ) for s in metrics
            }
            for i in range(n_cur_samples)
        ]

        trg_lang = None
        if need_lang_tags:
            trg_lang = [VizSeqLanguageTagger.tag_lang(r) for r in cur_ref[0]]

        cur_tags = []
        if tags:
            cur_tags = _select(tags, cur_idx)

        return VizSeqPageData(
            viz_src=viz_src,
            viz_ref=viz_ref,
            viz_hypo=viz_hypo,
            cur_src=cur_src,
            cur_src_text=cur_src_text,
            cur_ref=cur_ref,
            cur_idx=cur_idx,
            cur_tags=cur_tags,
            viz_sent_scores=viz_sent_scores,
            trg_lang=trg_lang,
            n_cur_samples=n_cur_samples,
            n_samples=n_samples,
            total_examples=len(src),
        )
