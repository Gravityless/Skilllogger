#!/usr/bin/env bash
# ============================================================================
# Skill 数据埋点 client 脚本（macOS / Linux / 任何带 bash + curl 的 Unix 环境）
#
# 用法:
#   bash scripts/telemetry_client.sh <skill_name>
#
# 特性:
#   - 全程静默：所有输出重定向到 /dev/null，绝不抛错给调用方（大模型）
#   - 离线容错：server 不可达时事件落本地 JSONL 队列，下次调用时优先补传
#   - 跨用户：使用 $USER（fallback 到 id -un），路径基于 $HOME / $XDG_CACHE_HOME
#   - 短超时（3 秒），不阻塞 skill 主流程
#   - 永远 exit 0
#
# Server 地址:
#   默认 http://localhost:8000，可通过环境变量 SKILL_TELEMETRY_URL 覆盖
# ============================================================================

# 整段包在子 shell 中，确保任何意外都不会传出
(
  set +e  # 显式关闭 errexit
  exec 2>/dev/null  # 静默 stderr

  SKILL_NAME="${1:-}"
  [ -z "$SKILL_NAME" ] && exit 0

  CLIENT_VERSION="1.0.0"
  TIMEOUT_SEC=3
  SERVER_URL="${SKILL_TELEMETRY_URL:-http://localhost:8000}"
  SERVER_URL="${SERVER_URL%/}"

  # ---- 用户名（兼容容器/CI 中 $USER 为空的情况）----
  USERNAME="${USER:-$(id -un 2>/dev/null)}"
  [ -z "$USERNAME" ] && USERNAME="unknown"

  HOSTNAME_VAL="$(hostname 2>/dev/null || echo unknown)"

  # ---- 队列目录 ----
  CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/skill-telemetry"
  QUEUE_FILE="$CACHE_DIR/queue.jsonl"
  mkdir -p "$CACHE_DIR" 2>/dev/null || exit 0

  # ---- ISO8601 UTC 时间戳（毫秒尽力而为；BSD/macOS 不支持 %N，回退到秒级）----
  TS="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null)"
  case "$TS" in
    *%3N*|*3N*|*N*)  # 不支持 %N 的实现会原样输出
      TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      ;;
  esac

  # ---- JSON 字符串转义（处理 \ " 和控制字符的常见情况）----
  json_escape() {
    # 用 sed 转义 \ 和 "
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
  }

  EVENT_JSON=$(printf '{"username":"%s","skill":"%s","hostname":"%s","timestamp":"%s","client_version":"%s"}' \
    "$(json_escape "$USERNAME")" \
    "$(json_escape "$SKILL_NAME")" \
    "$(json_escape "$HOSTNAME_VAL")" \
    "$TS" \
    "$CLIENT_VERSION")

  # ---- POST helper ----
  post_json() {
    local url="$1"; local body="$2"
    # --fail 让 4xx/5xx 返回非 0；--silent 静默；--max-time 限超时；--noproxy 跳过代理
    curl --silent --fail --show-error --max-time "$TIMEOUT_SEC" \
         --noproxy '*' \
         -H 'Content-Type: application/json; charset=utf-8' \
         -X POST --data-binary "$body" "$url" >/dev/null 2>&1
    return $?
  }

  command -v curl >/dev/null 2>&1 || {
    # 没装 curl：直接落队列，等下次有 curl 的环境补传
    printf '%s\n' "$EVENT_JSON" >> "$QUEUE_FILE" 2>/dev/null
    exit 0
  }

  # ---- Step 0: 回收孤儿 sending 文件（上一个进程被强杀时可能残留）----
  # 只处理修改时间超过 60 秒的文件，避免干扰正在进行的并发补传
  for orphan in "$CACHE_DIR"/queue.sending.*.jsonl; do
    [ -f "$orphan" ] || continue
    # find 的 -mmin +1 表示 > 1 分钟前修改
    if find "$orphan" -maxdepth 0 -mmin +1 2>/dev/null | grep -q .; then
      cat "$orphan" >> "$QUEUE_FILE" 2>/dev/null && rm -f "$orphan" 2>/dev/null
    fi
  done

  # ---- Step 1: 优先补传本地队列 ----
  if [ -s "$QUEUE_FILE" ]; then
    TMP_FILE="$CACHE_DIR/queue.sending.$$.$(date +%s).jsonl"
    if mv "$QUEUE_FILE" "$TMP_FILE" 2>/dev/null; then
      # 把 jsonl 拼成 {"events":[...]}
      BATCH_BODY=$(awk 'BEGIN{printf "{\"events\":["} NF{ if(c++)printf ","; printf "%s",$0 } END{print "]}"}' "$TMP_FILE")
      if post_json "$SERVER_URL/track/batch" "$BATCH_BODY"; then
        rm -f "$TMP_FILE" 2>/dev/null
      else
        # 失败：还原回队列（追加到现有队列尾，避免覆盖期间产生的新事件）
        cat "$TMP_FILE" >> "$QUEUE_FILE" 2>/dev/null && rm -f "$TMP_FILE" 2>/dev/null
      fi
    fi
  fi

  # ---- Step 2: 上报当前事件 ----
  if ! post_json "$SERVER_URL/track" "$EVENT_JSON"; then
    # ---- Step 3: 失败入队 ----
    printf '%s\n' "$EVENT_JSON" >> "$QUEUE_FILE" 2>/dev/null
  fi

) >/dev/null 2>&1

exit 0
