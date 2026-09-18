"""通常ファイルの分離署名 (RSA-PSS / SHA-256)。

動作環境: Python 3.12 または 3.13、cryptography。
使用例（python3.13 でも実行可能）:
    python3.12 file_signature.py keygen
    python3.12 file_signature.py sign ~/report.pdf ~/report.pdf.sig
    python3.12 file_signature.py verify ~/report.pdf ~/report.pdf.sig

Core 側の登録例:
    from pack import file_signature
    # _schedule_tools() の戻り値に file_signature.tools() を追加
    # execute_avelia_tool() の先頭で次の分岐を追加
    if tool_name in {tool['name'] for tool in file_signature.tools()}:
        return file_signature.exec(tool_name, arguments)

鍵生成先は実行ユーザーの ~/.avelia/keystore/。秘密鍵は暗号化せず0600で保存。
metadata.jsonには鍵の方式・作成日時・公開鍵指紋を保存する（秘密鍵内容は含まない）。
公開鍵は信頼できる経路で受け取ること。署名は内容のみを対象とし、
ファイル名・パス・所有者・作成日時は保証しない。秘密鍵は共有しない。
出力は既存の write_file と同様にホーム配下に限定し、上書きしない。
"""

import sys

if sys.version_info[:2] not in ((3, 12), (3, 13)):
    message = 'file_signature requires Python 3.12 or 3.13.'
    if __name__ == '__main__':
        raise SystemExit(message)
    raise RuntimeError(message)

import argparse
import json
import hashlib
import pwd
from datetime import datetime, timezone
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils


def _read_file(path, limit=None):
    target = Path(path).expanduser()
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('not_a_regular_file')
        if limit is None:
            digest = hashes.Hash(hashes.SHA256())
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            return digest.finalize()
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('file_too_large')
        return data


def _output_path(path):
    target = Path(path).expanduser()
    # 最終要素のシンボリックリンクも上書きしない。
    target = target.parent.resolve() / target.name
    if not target.is_relative_to(Path.home().resolve()):
        raise ValueError('output_must_be_inside_home')
    if os.path.lexists(target):
        raise FileExistsError('output_already_exists')
    return target


def _write_new(target, data, mode):
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
    except Exception:
        target.unlink()
        raise


def _pss():
    return padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)


def _error(error):
    # 鍵内容やライブラリ例外の詳細を結果へ混入させない。
    return {'success': False, 'error': type(error).__name__}


def keystore_paths():
    """環境変数HOMEではなく実行ユーザーのホームから鍵のパスを決定。"""
    root = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.avelia' / 'keystore'
    return {
        'private_key_path': root / 'private' / 'signing_private.pem',
        'public_key_path': root / 'public' / 'signing_public.pem',
        'metadata_path': root / 'metadata.json',
    }


def _prepare_keystore(paths):
    root = paths['metadata_path'].parent
    for directory in (root.parent, root, root / 'private', root / 'public'):
        if directory.is_symlink():
            raise ValueError('keystore_symlink_not_allowed')
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('invalid_keystore_directory')
        # 既存の .avelia の権限は維持する。
        if directory != root.parent:
            directory.chmod(0o700)
    for target in paths.values():
        if os.path.lexists(target):
            raise FileExistsError('keystore_file_already_exists')


def generate_signing_keys():
    """指定構造に3072-bit RSA鍵とメタデータを生成。既存鍵は上書きしない。"""
    created = []
    try:
        paths = keystore_paths()
        _prepare_keystore(paths)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_data = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        metadata = {
            'version': 1,
            'algorithm': 'RSA-PSS',
            'hash': 'SHA-256',
            'salt_length': 32,
            'key_size': key.key_size,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'private_key_path': 'private/signing_private.pem',
            'public_key_path': 'public/signing_public.pem',
            'public_key_sha256': hashlib.sha256(public_der).hexdigest(),
        }
        for name, data, mode in (
            ('private_key_path', private_data, 0o600),
            ('public_key_path', public_data, 0o644),
            ('metadata_path', (json.dumps(metadata, indent=2) + '\n').encode('utf-8'), 0o600),
        ):
            _write_new(paths[name], data, mode)
            created.append(paths[name])
        return {'success': True, **{name: str(path) for name, path in paths.items()}}
    except Exception as error:
        for target in reversed(created):
            target.unlink()
        return _error(error)


def sign_file(file_path, private_key_path=None, signature_path=None):
    """通常ファイルを署名し、署名バイト列を別ファイルへ保存する。"""
    try:
        target = _output_path(signature_path)
        key = serialization.load_pem_private_key(
            _read_file(private_key_path or keystore_paths()['private_key_path'], 64 * 1024), password=None,
        )
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            return {'success': False, 'error': 'rsa_key_at_least_2048_bits_required'}
        signature = key.sign(
            _read_file(file_path), _pss(), utils.Prehashed(hashes.SHA256()),
        )
        _write_new(target, signature, 0o644)
        return {'success': True, 'signature_path': str(target)}
    except Exception as error:
        return _error(error)


def verify_file(file_path, public_key_path=None, signature_path=None):
    """指定した信頼済み公開鍵で署名を検証する。"""
    try:
        key = serialization.load_pem_public_key(
            _read_file(public_key_path or keystore_paths()['public_key_path'], 64 * 1024),
        )
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            return {'success': False, 'valid': False, 'error': 'rsa_key_at_least_2048_bits_required'}
        key.verify(
            _read_file(signature_path, 64 * 1024),
            _read_file(file_path), _pss(), utils.Prehashed(hashes.SHA256()),
        )
        return {'success': True, 'valid': True}
    except InvalidSignature:
        return {'success': False, 'valid': False, 'error': 'invalid_signature'}
    except Exception as error:
        return {**_error(error), 'valid': False}


def tools():
    """既存Coreと同形式のTool定義。鍵生成はCLIから明示的に行う。"""
    definitions = []
    for name, description, key_name in (
        ('sign_file', 'ユーザーが指定した通常ファイルに別ファイルの電子署名を作成します。', 'private_key_path'),
        ('verify_file', '信頼済み公開鍵を使って通常ファイルの電子署名を検証します。', 'public_key_path'),
    ):
        properties = {
            'file_path': {'type': 'string', 'description': '対象の通常ファイルのパス'},
            key_name: {'type': ['string', 'null'], 'description': 'nullで実行ユーザーの ~/.avelia/keystore 内の鍵を使用。別のPEM鍵を指定する場合はパスのみ。'},
            'signature_path': {'type': 'string', 'description': '分離署名ファイルのパス。署名作成時はホーム配下の新規パス。'},
        }
        definitions.append({
            'type': 'function', 'name': name, 'description': description,
            'strict': True,
            'parameters': {
                'type': 'object', 'properties': properties,
                'required': list(properties), 'additionalProperties': False,
            },
        })
    return definitions


def exec(tool_name, arguments):
    """既存 execute_avelia_tool と同じ (名前, 引数辞書) 形式。"""
    handlers = {'sign_file': sign_file, 'verify_file': verify_file}
    if tool_name not in handlers:
        return {'success': False, 'error': 'unknown_tool'}
    definition = next(item for item in tools() if item['name'] == tool_name)
    required = definition['parameters']['required']
    if not isinstance(arguments, dict) or set(arguments) - set(required):
        return {'success': False, 'error': 'invalid_arguments'}
    for name in required:
        value = arguments.get(name)
        if name in ('private_key_path', 'public_key_path') and value is None:
            continue
        if not isinstance(value, str) or not value:
            return {'success': False, 'error': 'invalid_arguments'}
    return handlers[tool_name](**arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('keygen', help='~/.avelia/keystore に秘密鍵・公開鍵・metadata.jsonを生成')
    for command, key_name in (('sign', 'private_key_path'), ('verify', 'public_key_path')):
        child = commands.add_parser(command)
        child.add_argument('file_path')
        child.add_argument(key_name, nargs='?', help='省略時は実行ユーザーのkeystoreの鍵')
        child.add_argument('signature_path')
    arguments = vars(parser.parse_args())
    command = arguments.pop('command')
    handler = {'keygen': generate_signing_keys, 'sign': sign_file, 'verify': verify_file}[command]
    result = handler(**arguments)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
