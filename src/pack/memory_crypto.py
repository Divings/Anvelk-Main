# memory_crypto.py

import os
import json

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"VLM1"
NONCE_SIZE = 12


def create_key(key_file):
    """
    AES-256用の鍵を新規作成する。
    """

    key = AESGCM.generate_key(
        bit_length=256
    )

    os.makedirs(
        os.path.dirname(key_file),
        exist_ok=True
    )

    with open(key_file, "wb") as f:
        f.write(key)

    return key


def load_key(key_file):
    """
    AES-256鍵を読み込む。
    存在しない場合は新規作成する。
    """

    if not os.path.isfile(key_file):
        return create_key(key_file)

    with open(key_file, "rb") as f:
        key = f.read()

    if len(key) != 32:
        raise ValueError(
            "暗号化キーの形式が正しくありません。"
        )

    return key


def encrypt_memory(messages, memory_file, key_file):
    """
    会話履歴をAES-256-GCMで暗号化して保存する。
    """

    key = load_key(key_file)

    aesgcm = AESGCM(key)

    nonce = os.urandom(
        NONCE_SIZE
    )

    json_data = json.dumps(
        messages,
        ensure_ascii=False
    ).encode(
        "utf-8"
    )

    encrypted = aesgcm.encrypt(
        nonce,
        json_data,
        None
    )

    os.makedirs(
        os.path.dirname(memory_file),
        exist_ok=True
    )

    with open(memory_file, "wb") as f:
        f.write(MAGIC)
        f.write(nonce)
        f.write(encrypted)


def decrypt_memory(memory_file, key_file):
    """
    AES-256-GCMで暗号化された会話履歴を復号する。
    """

    if not os.path.isfile(memory_file):
        return []

    key = load_key(key_file)

    with open(memory_file, "rb") as f:

        magic = f.read(
            len(MAGIC)
        )

        if magic != MAGIC:
            raise ValueError(
                "memory.vlmの形式が正しくありません。"
            )

        nonce = f.read(
            NONCE_SIZE
        )

        encrypted = f.read()

    aesgcm = AESGCM(key)

    decrypted = aesgcm.decrypt(
        nonce,
        encrypted,
        None
    )

    messages = json.loads(
        decrypted.decode(
            "utf-8"
        )
    )

    if not isinstance(messages, list):
        raise ValueError(
            "会話履歴の形式が正しくありません。"
        )

    return messages