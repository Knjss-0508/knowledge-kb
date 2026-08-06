from __future__ import annotations

import argparse
import gc
import json
import math
from pathlib import Path
from typing import Any

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--candidate-model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--block-threshold", type=float, default=0.96)
    parser.add_argument("--minimum-recall-at-10", type=float, default=0.8)
    parser.add_argument("--maximum-false-block-rate", type=float, default=0.01)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-length", type=int, default=1024)
    return parser.parse_args()


def query_text(row: dict[str, Any]) -> str:
    messages = row.get("messages") or []
    raw_query = ""
    if messages and isinstance(messages[0], dict):
        raw_query = str(messages[0].get("content") or "")
    prompt = str(row.get("prompt") or "").strip()
    return f"Instruct: {prompt}\nQuery:{raw_query}" if prompt else raw_query


def last_token_pool(
    last_hidden_states: torch.Tensor,
    attention_mask: torch.Tensor,
) -> torch.Tensor:
    left_padding = bool((attention_mask[:, -1].sum() == attention_mask.shape[0]).item())
    if left_padding:
        return last_hidden_states[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = last_hidden_states.shape[0]
    return last_hidden_states[
        torch.arange(batch_size, device=last_hidden_states.device),
        sequence_lengths,
    ]


def load_encoder(model_name: str):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(
        model_name,
        padding_side="left",
        trust_remote_code=True,
    )
    model = AutoModel.from_pretrained(
        model_name,
        torch_dtype=dtype,
        trust_remote_code=True,
    )
    model.to(device)
    model.eval()
    return tokenizer, model, device


def encode(
    tokenizer,
    model,
    device: str,
    texts: list[str],
    *,
    batch_size: int,
    max_length: int,
) -> torch.Tensor:
    chunks: list[torch.Tensor] = []
    with torch.inference_mode():
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            inputs = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            inputs = {key: value.to(device) for key, value in inputs.items()}
            output = model(**inputs)
            embedding = last_token_pool(
                output.last_hidden_state,
                inputs["attention_mask"],
            )
            chunks.append(F.normalize(embedding.float(), p=2, dim=1).cpu())
    return torch.cat(chunks, dim=0)


def corpus_for_rows(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    corpus: list[str] = []
    index: dict[str, int] = {}
    for row in rows:
        for value in [*(row.get("positive") or []), *(row.get("negative") or [])]:
            text = str(value or "").strip()
            if text and text not in index:
                index[text] = len(corpus)
                corpus.append(text)
    return corpus, index


def calculate_metrics(
    rows: list[dict[str, Any]],
    query_embeddings: torch.Tensor,
    corpus_embeddings: torch.Tensor,
    corpus_index: dict[str, int],
    *,
    block_threshold: float,
) -> dict[str, Any]:
    ranks: list[int] = []
    positive_scores: list[float] = []
    top_negative_scores: list[float] = []
    dedup_negative_scores: list[float] = []

    for row_index, row in enumerate(rows):
        positive_values = [
            str(value or "").strip()
            for value in row.get("positive") or []
            if str(value or "").strip()
        ]
        if not positive_values:
            continue
        positive_index = corpus_index[positive_values[0]]
        scores = torch.mv(corpus_embeddings, query_embeddings[row_index])
        sorted_indices = torch.argsort(scores, descending=True)
        rank = int(
            (sorted_indices == positive_index).nonzero(as_tuple=False)[0].item()
        ) + 1
        ranks.append(rank)
        positive_scores.append(float(scores[positive_index].item()))

        explicit_negative_indices = [
            corpus_index[text]
            for text in (
                str(value or "").strip()
                for value in row.get("negative") or []
            )
            if text and text in corpus_index
        ]
        if explicit_negative_indices:
            top_negative = max(
                float(scores[index].item())
                for index in explicit_negative_indices
            )
            top_negative_scores.append(top_negative)
            if row.get("task_type") == "deduplication":
                dedup_negative_scores.extend(
                    float(scores[index].item())
                    for index in explicit_negative_indices
                )

    total = len(ranks)

    def recall(k: int) -> float:
        return round(sum(rank <= k for rank in ranks) / total, 6) if total else 0.0

    metrics = {
        "queries": total,
        "recall_at_1": recall(1),
        "recall_at_3": recall(3),
        "recall_at_5": recall(5),
        "recall_at_10": recall(10),
        "mrr": (
            round(sum(1.0 / rank for rank in ranks) / total, 6)
            if total
            else 0.0
        ),
        "ndcg_at_10": (
            round(
                sum(
                    1.0 / math.log2(rank + 1)
                    if rank <= 10
                    else 0.0
                    for rank in ranks
                )
                / total,
                6,
            )
            if total
            else 0.0
        ),
        "mean_positive_score": (
            round(sum(positive_scores) / len(positive_scores), 6)
            if positive_scores
            else None
        ),
        "mean_top_negative_score": (
            round(sum(top_negative_scores) / len(top_negative_scores), 6)
            if top_negative_scores
            else None
        ),
        "dedup_negative_pairs": len(dedup_negative_scores),
        "dedup_false_block_rate": (
            round(
                sum(score >= block_threshold for score in dedup_negative_scores)
                / len(dedup_negative_scores),
                6,
            )
            if dedup_negative_scores
            else None
        ),
    }
    return metrics


def evaluate_model(
    model_name: str,
    rows: list[dict[str, Any]],
    *,
    batch_size: int,
    max_length: int,
    block_threshold: float,
) -> dict[str, Any]:
    tokenizer, model, device = load_encoder(model_name)
    queries = [query_text(row) for row in rows]
    corpus, corpus_index = corpus_for_rows(rows)
    query_embeddings = encode(
        tokenizer,
        model,
        device,
        queries,
        batch_size=batch_size,
        max_length=max_length,
    )
    corpus_embeddings = encode(
        tokenizer,
        model,
        device,
        corpus,
        batch_size=batch_size,
        max_length=max_length,
    )
    dimension = int(query_embeddings.shape[1])
    split_metrics = {}
    for split in ("validation", "test"):
        split_indices = [
            index for index, row in enumerate(rows) if row.get("split") == split
        ]
        split_rows = [rows[index] for index in split_indices]
        split_queries = query_embeddings[split_indices]
        split_metrics[split] = calculate_metrics(
            split_rows,
            split_queries,
            corpus_embeddings,
            corpus_index,
            block_threshold=block_threshold,
        )
    del model, tokenizer, query_embeddings, corpus_embeddings
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return {
        "model": model_name,
        "dimension": dimension,
        **split_metrics,
    }


def main() -> None:
    args = parse_args()
    rows = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    rows = [
        row
        for row in rows
        if row.get("split") in {"validation", "test"}
        and row.get("positive")
    ]
    if not rows:
        raise RuntimeError("Evaluation dataset is empty")

    baseline = evaluate_model(
        args.base_model,
        rows,
        batch_size=args.batch_size,
        max_length=args.max_length,
        block_threshold=args.block_threshold,
    )
    candidate = evaluate_model(
        args.candidate_model,
        rows,
        batch_size=args.batch_size,
        max_length=args.max_length,
        block_threshold=args.block_threshold,
    )
    baseline_test = baseline["test"]
    candidate_test = candidate["test"]
    false_block_rate = candidate_test.get("dedup_false_block_rate")
    checks = {
        "dimension_preserved": candidate["dimension"] == baseline["dimension"],
        "recall_at_10_minimum": (
            candidate_test["recall_at_10"] >= args.minimum_recall_at_10
        ),
        "recall_at_10_no_regression": (
            candidate_test["recall_at_10"] >= baseline_test["recall_at_10"]
        ),
        "false_block_rate_limit": (
            True
            if false_block_rate is None
            else false_block_rate <= args.maximum_false_block_rate
        ),
    }
    result = {
        "baseline": baseline,
        "candidate": candidate,
        "improvement": {
            "recall_at_1": round(
                candidate_test["recall_at_1"] - baseline_test["recall_at_1"],
                6,
            ),
            "recall_at_10": round(
                candidate_test["recall_at_10"] - baseline_test["recall_at_10"],
                6,
            ),
            "mrr": round(candidate_test["mrr"] - baseline_test["mrr"], 6),
            "ndcg_at_10": round(
                candidate_test["ndcg_at_10"] - baseline_test["ndcg_at_10"],
                6,
            ),
        },
        "quality_gate": {
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "thresholds": {
                "minimum_recall_at_10": args.minimum_recall_at_10,
                "maximum_false_block_rate": args.maximum_false_block_rate,
                "dedup_block_threshold": args.block_threshold,
            },
        },
    }
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
