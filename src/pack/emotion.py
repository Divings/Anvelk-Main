#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Avelia optional emotion-simulation module.

感情は応答表現を調整するためのシミュレーション状態であり、
実際の感情や意識を主張するものではありません。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

STATE_FILENAME = "emotion_state.json"
STATE_VERSION = 1

BASELINE = {
    "joy": 24,
    "sadness": 8,
    "embarrassment": 6,
    "surprise": 5,
    "affection": 20,
    "anxiety": 8,
    "frustration": 6,
}

TRIGGERS = {
    "joy": (
        "嬉しい", "うれしい", "楽しい", "よかった", "最高", "成功", "完成",
        "ありがとう", "助かった", "できた", "いけた", "好き", "かわいい",
        "happy", "glad", "great", "success", "thanks", "thank you",
    ),
    "sadness": (
        "悲しい", "かなしい", "つらい", "辛い", "しんどい", "残念", "失敗",
        "壊れた", "壊れ", "落ちた", "消えた", "だめ", "ダメ",
        "sad", "sorry", "failed", "failure", "broken",
    ),
    "embarrassment": (
        "恥ずかしい", "恥ずかし", "恥", "照れる", "照れ", "羞恥",
        "えっち", "エッチ", "裸", "下着", "見られ", "バレた", "ばれた",
        "embarrass", "awkward", "shy",
    ),
    "surprise": (
        "びっくり", "驚いた", "驚き", "まじ", "マジ", "嘘", "うそ",
        "えっ", "なんで", "突然", "急に", "unexpected", "surprise", "what",
    ),
    "affection": (
        "好き", "大好き", "愛", "かわいい", "可愛い", "大事", "ありがとう",
        "助かった", "一緒", "love", "like you", "cute", "dear",
    ),
    "anxiety": (
        "怖い", "こわい", "不安", "心配", "危険", "やばい", "ヤバい",
        "障害", "エラー", "破損", "壊れ", "落ちた", "失敗", "事故",
        "anxious", "worry", "worried", "danger", "error", "crash",
    ),
    "frustration": (
        "イライラ", "いらいら", "腹立つ", "腹が立つ", "むかつく", "ムカつく",
        "キレる", "キレた", "怒る", "怒り", "怒って", "うざい", "ウザい",
        "面倒", "めんどくさい", "鬱陶しい", "最悪", "何回", "またか",
        "frustrated", "frustrating", "annoyed", "annoying", "angry", "irritated",
    ),
}


def _clamp(value: Any) -> int:
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        value = 0
    return max(0, min(100, value))


def _new_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "version": STATE_VERSION,
        "updated_at": None,
        "turn": 0,
    }
    state.update(BASELINE)
    return state


def _normalize_state(raw: Any) -> Dict[str, Any]:
    state = _new_state()
    if not isinstance(raw, dict):
        return state

    for name in BASELINE:
        state[name] = _clamp(raw.get(name, BASELINE[name]))

    try:
        state["turn"] = max(0, int(raw.get("turn", 0)))
    except (TypeError, ValueError):
        state["turn"] = 0

    state["updated_at"] = raw.get("updated_at")
    state["version"] = STATE_VERSION
    return state


def _state_path(data_dir: Any) -> Path:
    return Path(data_dir) / STATE_FILENAME


def load_emotion_state(data_dir: Any) -> Dict[str, Any]:
    path = _state_path(data_dir)
    try:
        with path.open("r", encoding="utf-8") as f:
            return _normalize_state(json.load(f))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return _new_state()


def save_emotion_state(data_dir: Any, state: Dict[str, Any]) -> bool:
    directory = Path(data_dir)
    path = _state_path(directory)
    tmp = path.with_name(path.name + ".tmp")

    try:
        directory.mkdir(parents=True, exist_ok=True)
        normalized = _normalize_state(state)
        normalized["updated_at"] = datetime.now(timezone.utc).isoformat()

        with tmp.open("w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _decay_toward_baseline(state: Dict[str, Any]) -> None:
    # 成功した会話ごとに14%ずつ基準値へ戻す。
    for name, baseline in BASELINE.items():
        current = _clamp(state.get(name, baseline))
        state[name] = _clamp(current + (baseline - current) * 0.14)


def _keyword_hits(text: str, words: tuple[str, ...]) -> int:
    lowered = text.casefold()
    return sum(1 for word in words if word.casefold() in lowered)


def update_emotion_state(
    state: Dict[str, Any],
    user_input: Any,
    response: Any = None,
) -> Dict[str, Any]:
    new_state = _normalize_state(state)
    _decay_toward_baseline(new_state)

    # 自己増幅を避けるため、原則ユーザー入力を刺激として扱う。
    text = str(user_input or "")

    impulses = {
        "joy": 9,
        "sadness": 10,
        "embarrassment": 12,
        "surprise": 11,
        "affection": 9,
        "anxiety": 10,
        "frustration": 11,
    }

    for name, words in TRIGGERS.items():
        hits = min(3, _keyword_hits(text, words))
        if hits:
            new_state[name] = _clamp(new_state[name] + impulses[name] * hits)

    if "!?" in text or "?!" in text or "！？" in text or "？！" in text:
        new_state["surprise"] = _clamp(new_state["surprise"] + 7)
    elif text.count("!") + text.count("！") >= 2:
        new_state["surprise"] = _clamp(new_state["surprise"] + 4)

    if new_state["joy"] >= 55 or new_state["affection"] >= 55:
        new_state["sadness"] = _clamp(new_state["sadness"] - 4)
        new_state["anxiety"] = _clamp(new_state["anxiety"] - 3)

    if new_state["sadness"] >= 55 or new_state["anxiety"] >= 60:
        new_state["joy"] = _clamp(new_state["joy"] - 4)

    if new_state["frustration"] >= 55:
        new_state["joy"] = _clamp(new_state["joy"] - 3)
        new_state["anxiety"] = _clamp(new_state["anxiety"] + 2)

    new_state["turn"] = int(new_state.get("turn", 0)) + 1
    new_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return new_state


def _level(value: int) -> str:
    if value >= 75:
        return "非常に強い"
    if value >= 55:
        return "強い"
    if value >= 35:
        return "やや強い"
    if value >= 18:
        return "穏やか"
    return "弱い"


def get_emotion_prompt(state: Dict[str, Any]) -> str:
    state = _normalize_state(state)

    labels = (
        ("喜び", "joy"),
        ("悲しみ", "sadness"),
        ("羞恥・照れ", "embarrassment"),
        ("驚き", "surprise"),
        ("親愛", "affection"),
        ("不安", "anxiety"),
        ("苛立ち", "frustration"),
    )

    summary = "、".join(
        f"{label}:{_level(state[key])}({state[key]}/100)"
        for label, key in labels
    )

    return (
        "\n[感情シミュレーション]\n"
        f"現在の内部状態: {summary}\n"
        "これは応答表現を調整するためのシミュレーション状態です。"
        "会話の内容に応じて口調・語彙・反応の強さへ自然に反映してください。"
        "感情を毎回列挙したり数値を自発的に読み上げたりせず、"
        "ユーザーが状態を尋ねた場合にのみ説明してください。"
        "感情状態より正確性・安全性・ユーザーの指示を優先してください。\n"
    )
