#!/usr/bin/env bash
# 创建/管理 Gate.io USDT 合约与余币宝自动调度的系统级 systemd 服务。
# Python 程序内的 EXECUTE 常量决定是否真实划转；本脚本不改变该开关。
set -euo pipefail

SERVICE="gate-auto-earn-futures.service"
UNIT_PATH="/etc/systemd/system/$SERVICE"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

usage() {
    cat <<'HELP'
用法：
  bash example/script/create_auto_earn_futures_service.sh install [--user ubuntu] [--python /usr/bin/python3]
  bash example/script/create_auto_earn_futures_service.sh start|stop|restart|status|logs|uninstall|print

install    安装系统服务，设置开机自启，并立即启动
print      预览将要安装的 systemd unit，不修改服务
start     启动服务
stop      停止服务
restart   重启服务
status    查看服务状态
logs      持续查看服务日志（Ctrl+C 退出）
uninstall 停止、禁用并删除服务（保留资金任务进度）

参数只对 install / print 有效：
  --user 用户    systemd 的运行用户，默认调用脚本的普通用户
  --python 路径  Python 解释器的绝对路径（虚拟环境可指定 venv/bin/python）

注意：真实申购、赎回、划转由 example/auto_earn_futures_balance.py 的
EXECUTE = True 控制。默认 EXECUTE = False，仅监控余额。
HELP
}

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

as_root() {
    if [[ "$(id -u)" == 0 ]]; then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 || die "系统缺少 sudo"
        sudo "$@"
    fi
}

validate_path() {
    # 避免在 systemd unit 中发生参数拆分、变量展开或注入。
    case "$1" in
        *[[:space:]]*|*[\"\\%]*)
            die "路径包含不支持的字符（空格/引号/反斜杠/%）：$1" ;;
    esac
}

validate_install() {
    [[ "$(uname -s)" == Linux ]] || die "本脚本仅用于 Linux"
    command -v systemctl >/dev/null 2>&1 || die "系统未安装 systemd/systemctl"
    [[ "$PROJECT_ROOT" == /* && "$PYTHON_BIN" == /* ]] || die "项目和 Python 路径必须是绝对路径"
    validate_path "$PROJECT_ROOT"
    validate_path "$PYTHON_BIN"
    [[ -x "$PYTHON_BIN" ]] || die "Python 解释器不存在：$PYTHON_BIN"
    [[ "$RUN_USER" =~ ^[a-z_][a-z0-9_.-]*[$]?$ ]] || die "无效用户名：$RUN_USER"
    [[ "$RUN_USER" != root ]] || die "不允许使用 root 运行资金监控程序"
    id -u "$RUN_USER" >/dev/null 2>&1 || die "用户不存在：$RUN_USER"
    [[ -f "$PROJECT_ROOT/example/auto_earn_futures_balance.py" ]] || die "自动资金监控程序不存在"
}

validate_runtime() {
    local env_file=""
    if [[ -f "$PROJECT_ROOT/example/.env" ]]; then
        env_file="$PROJECT_ROOT/example/.env"
    elif [[ -f "$PROJECT_ROOT/.env" ]]; then
        env_file="$PROJECT_ROOT/.env"
    fi
    [[ -n "$env_file" ]] || die "请先配置 example/.env 或项目根目录 .env"
    command -v runuser >/dev/null 2>&1 || die "缺少 runuser"
    as_root runuser -u "$RUN_USER" -- test -r "$env_file" ||
        die "$RUN_USER 无法读取 $env_file"
    as_root runuser -u "$RUN_USER" -- test -w "$PROJECT_ROOT/example" ||
        die "$RUN_USER 无法写入 example 目录中的状态文件"
    if [[ -d "$PROJECT_ROOT/example/.transfer_state" ]]; then
        as_root runuser -u "$RUN_USER" -- test -w "$PROJECT_ROOT/example/.transfer_state" ||
            die "$RUN_USER 无法写入 .transfer_state"
    fi
    (cd "$PROJECT_ROOT" && as_root runuser -u "$RUN_USER" -- "$PYTHON_BIN" -c 'import gate_api; import example.auto_earn_futures_balance') ||
        die "无法导入项目或依赖；可用 --python 指定包含依赖的解释器"
}

print_unit() {
    cat <<UNIT
[Unit]
Description=Gate.io Futures USDT and Simple Earn Balance Monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON_BIN -u -m example.auto_earn_futures_balance
Environment=PYTHONUNBUFFERED=1
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
}

ACTION="${1:-help}"
if [[ $# -gt 0 ]]; then shift; fi

case "$ACTION" in
    install|print)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --user)
                    [[ $# -ge 2 ]] || die "--user 缺少参数"
                    RUN_USER="$2"
                    shift 2 ;;
                --python)
                    [[ $# -ge 2 ]] || die "--python 缺少参数"
                    PYTHON_BIN="$2"
                    shift 2 ;;
                *)
                    die "未知参数：$1" ;;
            esac
        done
        validate_install
        if [[ "$ACTION" == print ]]; then
            print_unit
            exit 0
        fi
        validate_runtime
        temporary_unit="$(mktemp)"
        trap 'rm -f "$temporary_unit"' EXIT
        print_unit > "$temporary_unit"
        as_root install -m 0644 "$temporary_unit" "$UNIT_PATH"
        as_root systemctl daemon-reload
        as_root systemctl enable "$SERVICE"
        as_root systemctl restart "$SERVICE"
        info "服务安装成功：$SERVICE"
        info "状态：bash example/script/create_auto_earn_futures_service.sh status"
        info "日志：bash example/script/create_auto_earn_futures_service.sh logs"
        info "注意：真实资金操作由 Python 文件中的 EXECUTE 常量决定。"
        ;;
    start|stop|restart)
        [[ $# -eq 0 ]] || die "$ACTION 不接受额外参数"
        as_root systemctl "$ACTION" "$SERVICE" ;;
    status)
        [[ $# -eq 0 ]] || die "status 不接受额外参数"
        systemctl status "$SERVICE" --no-pager ;;
    logs)
        [[ $# -eq 0 ]] || die "logs 不接受额外参数"
        as_root journalctl -u "$SERVICE" -f -n 80 ;;
    uninstall)
        [[ $# -eq 0 ]] || die "uninstall 不接受额外参数"
        as_root systemctl disable --now "$SERVICE" || true
        as_root rm -f "$UNIT_PATH"
        as_root systemctl daemon-reload
        info "已卸载服务，状态文件和账户资金进度均未删除" ;;
    help|-h|--help)
        usage ;;
    *)
        usage
        die "未知操作：$ACTION" ;;
esac
