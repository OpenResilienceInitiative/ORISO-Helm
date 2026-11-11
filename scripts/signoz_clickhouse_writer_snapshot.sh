#!/bin/sh
# Print a metadata fingerprint and writer states for the installed ClickHouse build.
# Keep config values inside this process. Unknown or changing evidence grants no exemption.
set -eu

data_dir=${1:-/var/lib/clickhouse}
config=${2:-/etc/clickhouse-server/config.xml}
status_file=$data_dir/status
startup_config=$data_dir/preprocessed_configs/config.xml

file_identity() {
    stat -Lc '%d:%i:%s:%y:%z' "$1"
}

section_absent() {
    code=0
    message=$(clickhouse extract-from-config --config-file="$1" --key="$2" --log-level=warning < "$3" 2>&1) || code=$?
    [ "$code" -eq 232 ] &&
        [ "$message" = "Poco::Exception. Code: 1000, e.code() = 0, Not found: $2 (version 25.12.5.44 (official build))" ]
}

writer_state() {
    if printf '%s\n' "$threads" | grep -Exq "$2"; then
        printf active
    elif section_absent /dev/stdin "$1" "$startup_config" &&
         section_absent "$config" "$1" /dev/null; then
        printf disabled
    else
        printf unverified
    fi
}

snapshot() {
    [ "$(uname -sm)" = "Linux x86_64" ] || return 1
    binary=$(command -v clickhouse) || return 1
    identity=$(file_identity "$status_file") || return 1
    pid=$(awk '$1 == "PID:" {print $2}' "$status_file") || return 1
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    proc=/proc/$pid
    [ "$proc/exe" -ef "$binary" ] || return 1
    [ "$(cat "$proc/comm")" = clickhouse-serv ] || return 1
    ticks=$(awk '{print $22}' "$proc/stat") || return 1
    started=$(LC_ALL=C TZ=UTC ps -p "$pid" -o lstart=) || return 1
    epoch=$(LC_ALL=C TZ=UTC date -d "$started" +%s) || return 1
    status_time=$(stat -Lc '%Y' "$status_file") || return 1
    merged_identity=$(file_identity "$startup_config") || return 1
    merged_time=$(stat -Lc '%Z' "$startup_config") || return 1
    threads=$(LC_ALL=C ps -T -p "$pid" -o comm=) || return 1
    expected_threads=$(awk '$1 == "Threads:" {print $2}' "$proc/status") || return 1
    [ "$(printf '%s\n' "$threads" | awk 'END {print NR}')" = "$expected_threads" ] || return 1
    printf '%s\n' "$threads" | grep -Fxq clickhouse-serv || return 1

    # A reload may save new XML before its updater runs. The boot window,
    # load counters and the actual collector threads all constrain the evidence.
    readback=$(clickhouse-client --host 127.0.0.1 --readonly=1 --log_queries=0 --max_execution_time=5 --query "
        SELECT version(),
          (SELECT value FROM system.build_options WHERE name = 'VERSION_GITHASH'),
          (SELECT value FROM system.events WHERE event = 'MainConfigLoads'),
          (SELECT value FROM system.events WHERE event = 'ServerStartupMilliseconds'),
          (SELECT sum(value) FROM system.errors WHERE name = 'CANNOT_LOAD_CONFIG')
        FORMAT TSV") || return 1
    set -- $readback
    [ "$#" -eq 5 ] && [ "$1" = 25.12.5.44 ] &&
        [ "$2" = 6b09ccf262fd16345434572fb4d45dfd608f86c2 ] &&
        [ "$3" = 1 ] && [ "$5" = 0 ] || return 1
    startup_ms=$4
    case "$startup_ms" in ''|0|*[!0-9]*) return 1 ;; esac
    [ "$status_time" -ge "$epoch" ] && [ "$merged_time" -ge "$status_time" ] &&
        [ "$merged_time" -le "$((epoch + (startup_ms + 999) / 1000 + 1))" ] || return 1

    metric=$(writer_state metric_log 'MetricLog|TMetricLog') || return 1
    trace=$(writer_state trace_log TraceCollector) || return 1

    [ "$identity" = "$(file_identity "$status_file")" ] &&
        [ "$merged_identity" = "$(file_identity "$startup_config")" ] &&
        [ "$ticks" = "$(awk '{print $22}' "$proc/stat")" ] || return 1
    fingerprint=$(printf '%s\n' "$pid" "$ticks" "$identity" "$merged_identity" | sha256sum) || return 1
    printf '%s\t%s\t%s\n' "${fingerprint%% *}" "$metric" "$trace"
}

snapshot 2>/dev/null || printf 'unverified\n'
