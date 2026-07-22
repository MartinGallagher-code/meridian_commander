#!/usr/bin/env bash
# merge.sh -- bundle a directory tree into a single text file.
#
# Usage: merge.sh [OUTPUT] [SOURCE_DIR]
#   OUTPUT      bundle file to write   (default: bundle.txt)
#   SOURCE_DIR  directory to bundle    (default: current directory)
#
# Companion of split.sh, which expands the bundle again.
#
# Format (v2) -- one section per entry:
#   ===FILE: rel/path===          text file, inlined verbatim
#   ===FILE-B64: rel/path===      binary (or marker-colliding) file, base64
#   ===DIR: rel/path===           empty directory
#   ===LINK: rel/path===          symlink; the target is the section body
#   ===META: mode=644 sha256=... nonl=1===
#                                 optional, directly after FILE/FILE-B64:
#                                 permissions, checksum, and a flag for files
#                                 with no trailing newline
#   ===END===                     closes every section
#
# Robustness properties:
#   * filenames with spaces/quotes/globs are safe (NUL-separated find);
#     filenames containing newlines or control characters are skipped
#     with a warning -- they cannot be represented in a line-based format
#   * a text file that itself contains "===...===" marker lines is stored
#     base64-encoded so it can never corrupt the bundle
#   * text files without a trailing newline round-trip exactly (nonl=1)
#   * empty files stay text (not base64), empty dirs and symlinks survive
#   * the output file is written atomically (tmp + rename) and never
#     bundles itself, even mid-write
#   * per-file sha256 recorded when a checksum tool is available, so
#     split.sh can verify integrity
#   * deterministic entry order (LC_ALL=C sort) -- same tree, same bundle

set -euo pipefail
export LC_ALL=C

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && usage 0

out="${1:-bundle.txt}"
src="${2:-.}"

[ -d "$src" ] || { echo "merge.sh: source directory not found: $src" >&2; exit 1; }

# Absolute paths for self-exclusion (output, its tempfile, and these scripts).
abspath() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *)  printf '%s/%s\n' "$PWD" "$1" ;;
    esac
}
out_abs=$(abspath "$out")
tmp_abs="$out_abs.tmp.$$"
script_abs=$(abspath "$0")
split_abs="$(dirname "$script_abs")/split.sh"
src_abs=$(cd "$src" && pwd)

# Checksum tool (optional -- bundles still work without one).
if command -v sha256sum >/dev/null 2>&1; then
    sha() { sha256sum <"$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha() { shasum -a 256 <"$1" | awk '{print $1}'; }
else
    sha() { :; }
fi

# Permission bits, portable across GNU and BSD stat.
mode_of() {
    stat -c '%a' "$1" 2>/dev/null && return
    stat -f '%Lp' "$1" 2>/dev/null && return
    if [ -x "$1" ]; then echo 755; else echo 644; fi
}

files=0 b64s=0 dirs=0 links=0 skipped=0

warn_skip() {
    printf 'merge.sh: skipping (%s): %s\n' "$2" "$1" >&2
    skipped=$((skipped + 1))
}

# True if the file is non-empty and its last byte is not a newline.
# (od instead of command substitution on the raw byte: a trailing NUL in a
# binary would otherwise make bash warn about ignored null bytes.)
lacks_final_newline() {
    [ -s "$1" ] || return 1
    [ "$(tail -c 1 "$1" | od -An -tx1 | tr -d ' \n')" != "0a" ]
}

emit_meta() {  # $1=file
    local mode sum extra=""
    mode=$(mode_of "$1")
    sum=$(sha "$1" || true)
    lacks_final_newline "$1" && extra=" nonl=1"
    printf '===META: mode=%s%s%s===\n' \
        "$mode" "${sum:+ sha256=$sum}" "$extra"
}

# Does this file need base64?  Binary content, or lines that would collide
# with our markers.  Empty files are plain text.
needs_b64() {  # $1=file
    [ -s "$1" ] || return 1
    grep -Iq . "$1" 2>/dev/null || return 0
    grep -qE '^===(FILE|FILE-B64|DIR|LINK|META|END)' "$1" 2>/dev/null && return 0
    return 1
}

sort_z() {
    if printf 'b\0a\0' | sort -z >/dev/null 2>&1; then sort -z; else cat; fi
}

trap 'rm -f "$tmp_abs"' EXIT

{
    printf '# bundle format v2 (merge.sh) -- expand with split.sh\n'

    while IFS= read -r -d '' f; do
        rel="${f#"$src"/}"
        [ "$rel" = "$f" ] && continue          # the source dir itself
        f_abs="$src_abs/$rel"
        case "$f_abs" in
            "$out_abs"|"$tmp_abs"|"$script_abs"|"$split_abs") continue ;;
        esac

        case "$rel" in
            *[$'\x01'-$'\x1f']*)
                warn_skip "$rel" "control character in name"; continue ;;
        esac

        if [ -L "$f" ]; then
            target=$(readlink "$f") || { warn_skip "$rel" "unreadable link"; continue; }
            case "$target" in
                *$'\n'*) warn_skip "$rel" "newline in link target"; continue ;;
            esac
            printf '===LINK: %s===\n%s\n===END===\n' "$rel" "$target"
            links=$((links + 1))
        elif [ -d "$f" ]; then
            printf '===DIR: %s===\n===END===\n' "$rel"
            dirs=$((dirs + 1))
        elif [ -f "$f" ]; then
            if [ ! -r "$f" ]; then warn_skip "$rel" "unreadable"; continue; fi
            if needs_b64 "$f"; then
                printf '===FILE-B64: %s===\n' "$rel"
                emit_meta "$f"
                base64 <"$f"
                printf '===END===\n'
                b64s=$((b64s + 1))
            else
                printf '===FILE: %s===\n' "$rel"
                emit_meta "$f"
                cat "$f"
                # Keep the closing marker on its own line even when the file
                # does not end with a newline; nonl=1 lets split.sh undo this.
                lacks_final_newline "$f" && printf '\n'
                printf '===END===\n'
            fi
            files=$((files + 1))
        fi
    done < <(
        find "$src" -name .git -prune -o \
            \( -type f -o -type l -o \( -type d -empty \) \) -print0 \
        | sort_z
    )
} > "$tmp_abs"

mv "$tmp_abs" "$out_abs"
trap - EXIT

total_lines=$(($(wc -l < "$out_abs")))
echo "Wrote $out: $files file(s) ($b64s base64), $dirs empty dir(s), \
$links symlink(s), $skipped skipped, $total_lines lines"
