#!/bin/zsh
# Accept explicit bash invocation by handing off before zsh-only syntax runs.
if [ -z "${ZSH_VERSION:-}" ]; then
  exec /bin/zsh "$0" "$@"
fi
# macOS: double-click in Finder, or run ./start.command [server arguments].
set -eu
set -o pipefail

fail() {
  print -u2 -- "启动失败：$1"
  if [[ -t 0 ]]; then
    read -r "?按回车关闭此窗口……" || true
  fi
  exit 1
}

launcher_dir="${0:A:h}"
# This is a standalone installation; relative paths use this project folder.
cd -- "$launcher_dir" || fail "无法进入前端目录。"
venv_python="$launcher_dir/.venv/bin/python3"

if [[ ! -x "$venv_python" ]]; then
  bootstrap_python=""
  # Finder's Terminal may have a different PATH from an interactive shell.
  for candidate in /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      bootstrap_python="$candidate"
      break
    fi
  done
  [[ -n "$bootstrap_python" ]] || fail "需要 Python 3.10 或更新版本；安装后重新双击此文件。"
  print -- "首次启动：在本前端目录创建 Python 环境 .venv……"
  "$bootstrap_python" -m venv "$launcher_dir/.venv" || fail "无法创建 Python 虚拟环境，请查看上方错误。"
fi

"$venv_python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || \
  fail "本目录的 .venv 不可用，或其 Python 版本低于 3.10。"

if ! "$venv_python" -c 'import serial; from serial.tools import list_ports' 2>/dev/null; then
  print -- "首次启动：安装串口依赖 pyserial（需要联网）……"
  "$venv_python" -m pip install --disable-pip-version-check --no-cache-dir \
    -r "$launcher_dir/requirements.txt" || fail "依赖安装失败，请检查网络并重试。"
fi

print -- "启动 ESP32-C3 Super Mini Duo v2 前端；请打开下方服务输出的网址。"
print -- "请保持这个终端窗口运行；结束时先在网页停止录制，再按 Control-C。"
export PYTHONUNBUFFERED=1
"$venv_python" "$launcher_dir/launch.py" "$@" || fail "网页服务启动失败，请查看上方错误。"
