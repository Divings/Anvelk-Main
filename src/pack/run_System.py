import os
import signal
import selectors
import subprocess
import time


DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120

DEFAULT_MAX_OUTPUT = 64 * 1024      # 64KB
MAX_OUTPUT_LIMIT = 1024 * 1024      # 最大1MB

def block_cmd(command):
    """
    コマンドの先頭がブロック対象かどうかを判定する。
    """
    block_list = [
        "rm",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init",
        "telinit",
        "service",
        "killall",
        "pkill"
    ]
    if not command:
        return False
    elif any(command.startswith(block_cmd) for block_cmd in block_list):
        return True
    
def run_system_command(
    command,
    timeout=DEFAULT_TIMEOUT,
    max_output=DEFAULT_MAX_OUTPUT
):
    """
    Linux上で任意のシェルコマンドを実行する。

    ・タイムアウトあり
    ・出力サイズ制限あり
    ・stdout / stderr はまとめて取得
    ・タイムアウトまたは出力超過時はプロセスグループを終了
    """

    command = str(command).strip()

    if not command:
        return {
            "success": False,
            "error": "empty_command"
        }

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    try:
        max_output = int(max_output)
    except (TypeError, ValueError):
        max_output = DEFAULT_MAX_OUTPUT

    timeout = max(1, min(timeout, MAX_TIMEOUT))
    max_output = max(1024, min(max_output, MAX_OUTPUT_LIMIT))

    start_time = time.monotonic()

    try:
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )

    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}"
        }

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    output = bytearray()
    timed_out = False
    output_truncated = False

    try:
        while True:

            elapsed = time.monotonic() - start_time

            if elapsed >= timeout:
                timed_out = True
                break

            events = selector.select(timeout=0.2)

            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 4096)

                if not chunk:
                    continue

                remaining = max_output - len(output)

                if remaining <= 0:
                    output_truncated = True
                    break

                if len(chunk) > remaining:
                    output.extend(chunk[:remaining])
                    output_truncated = True
                    break

                output.extend(chunk)

            if output_truncated:
                break

            if process.poll() is not None:

                # プロセス終了後、パイプに残った出力を回収
                while len(output) < max_output:
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(4096, max_output - len(output))
                    )

                    if not chunk:
                        break

                    output.extend(chunk)

                break

        if timed_out or output_truncated:

            try:
                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGTERM
                )

                process.wait(timeout=2)

            except Exception:
                try:
                    os.killpg(
                        os.getpgid(process.pid),
                        signal.SIGKILL
                    )
                except Exception:
                    pass

        else:
            process.wait()

    finally:
        selector.close()

    duration = round(
        time.monotonic() - start_time,
        3
    )

    text = output.decode(
        "utf-8",
        errors="replace"
    )

    if output_truncated:
        text += "\n\n[出力上限に達したため、以降を省略しました]"

    if timed_out:
        text += "\n\n[タイムアウトによりコマンドを終了しました]"

    exit_code = process.returncode

    return {
        "success": (
            not timed_out
            and not output_truncated
            and exit_code == 0
        ),
        "command": command,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_truncated": output_truncated,
        "duration_seconds": duration,
        "output": text
    }