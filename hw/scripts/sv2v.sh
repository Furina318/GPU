#!/bin/bash
set -euo pipefail

SV2V="${SV2V:-sv2v}"

top=""
source=""
includes=()
macro_args=()
output_file=out.v

usage() {
    cat <<'EOF'
Usage: $0 -s <source> [-t <top>] [-o <out.v>] [-I <dir>]... [-D <macro>]...
  -s  source file
  -t  top module name (--top)
  -o  output verilog file
  -I  include directory (repeatable)
  -D  macro definition (repeatable)
EOF
}

[ $# -eq 0 ] && { usage; exit 1; }

while getopts "t:s:o:I:D:h" arg; do
    case $arg in
        t) top="--top=${OPTARG}" ;;
        s) source="${OPTARG}" ;;
        o) output_file="${OPTARG}" ;;
        I) includes+=("${OPTARG}") ;;
        D) macro_args+=("-D${OPTARG}") ;;
        h) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done

inc_args=()
for dir in "${includes[@]}"; do
    inc_args+=("-I${dir}")
done

# 收集源文件：先主源，再 include 目录下的 .v/.sv
file_args=("$source")
for dir in "${includes[@]}"; do
    while IFS= read -r -d '' file; do
        echo "file: $file"
        file_args+=("$file")
    done < <(find "$dir" -maxdepth 1 -type f \( -name '*.v' -o -name '*.sv' \) -print0)
done

cmd=("$SV2V")
[[ -n "$top" ]] && cmd+=("$top")
cmd+=("${macro_args[@]}" "${inc_args[@]}" "${file_args[@]}" -v -w "$output_file")

echo "${cmd[*]}"
"${cmd[@]}"