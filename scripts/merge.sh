#!/usr/bin/env bash
# merge.sh -- bundle a directory tree into a single text file.
#
# Usage: merge.sh [-m SIZE] [OUTPUT] [SOURCE_DIR]
#   OUTPUT        bundle file to write   (default: bundle.txt)
#   SOURCE_DIR    directory to bundle    (default: current directory)
#   -m, --max-size SIZE
#                 split the bundle into parts no larger than SIZE, named
#                 bundle-part1-of-N.txt and so on.  SIZE takes a K, M or G
#                 suffix (400K, 2M) or a plain byte count.  Expand the parts
#                 into the same directory, in any order, to get the tree back.
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
#   * parts are cut only at section boundaries, so each one is a valid
#     bundle in its own right and split.sh needs no special handling; each
#     carries its own entry count, so a truncated part is still caught

set -euo pipefail
export LC_ALL=C

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# Parse SIZE: a byte count, optionally with a K, M or G suffix.
parse_size() {
    case "$1" in
        *[!0-9KMGkmg]*|"") return 1 ;;
    esac
    local number="${1%[KMGkmg]}" unit="${1#"${1%?}"}"
    case "$number" in ""|*[!0-9]*) return 1 ;; esac
    case "$unit" in
        [Kk]) echo $((number * 1024)) ;;
        [Mm]) echo $((number * 1024 * 1024)) ;;
        [Gg]) echo $((number * 1024 * 1024 * 1024)) ;;
        *)    echo "$number" ;;
    esac
}

max_size=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage 0 ;;
        -m|--max-size)
            [ $# -ge 2 ] || { echo "merge.sh: $1 needs a size" >&2; exit 2; }
            max_size=$(parse_size "$2") || {
                echo "merge.sh: not a size: $2" >&2; exit 2; }
            [ "$max_size" -gt 0 ] || {
                echo "merge.sh: size must be more than zero" >&2; exit 2; }
            shift 2 ;;
        --max-size=*)
            max_size=$(parse_size "${1#*=}") || {
                echo "merge.sh: not a size: ${1#*=}" >&2; exit 2; }
            shift ;;
        --) shift; break ;;
        -*) echo "merge.sh: unknown option: $1" >&2; usage 2 ;;
        *) break ;;
    esac
done

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

body_abs="$tmp_abs.body"
trap 'rm -f "$tmp_abs" "$body_abs"' EXIT

# The body is written first so the entry count is known, then the final
# bundle is assembled with the count in the header.  split.sh compares the
# header count against what it restored, which catches a bundle truncated
# exactly at a section boundary -- a cut that would otherwise parse cleanly.
{
    while IFS= read -r -d '' f; do
        rel="${f#"$src"/}"
        [ "$rel" = "$f" ] && continue          # the source dir itself
        f_abs="$src_abs/$rel"
        case "$f_abs" in
            # The body tempfile belongs here too: find runs concurrently with
            # the loop that writes it, so bundling into the tree being scanned
            # would otherwise inline the growing file into itself and never
            # terminate.
            "$out_abs"|"$tmp_abs"|"$body_abs"|"$script_abs"|"$split_abs") continue ;;
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
} > "$body_abs"

entries=$((files + dirs + links))

# Name for part N of M, with the number worked into the output file name
# before its extension: bundle.txt -> bundle-part2-of-5.txt.
part_name() {  # $1=index $2=total
    case "$out" in
        *.*) printf '%s-part%s-of%s.%s\n' "${out%.*}" "$1" "$2" "${out##*.}" ;;
        *)   printf '%s-part%s-of%s\n' "$out" "$1" "$2" ;;
    esac
}

if [ "$max_size" -eq 0 ]; then
    {
        printf '# bundle format v2 (merge.sh) -- expand with split.sh\n'
        printf '# bundle entries: %s\n' "$entries"
        cat "$body_abs"
    } > "$tmp_abs"
    rm -f "$body_abs"
    mv "$tmp_abs" "$out_abs"
    trap - EXIT
    total_lines=$(($(wc -l < "$out_abs")))
    echo "Wrote $out: $files file(s) ($b64s base64), $dirs empty dir(s), \
$links symlink(s), $skipped skipped, $total_lines lines"
    exit 0
fi

# -- splitting ---------------------------------------------------------------
#
# Cut only between sections: a part that ended mid-section would not be a
# bundle at all, and split.sh would rightly refuse it.  The first pass just
# measures, so the total is known before any part is named -- the names carry
# "of N", and N cannot be guessed at.

# Every part carries its own header, which has to come out of its budget.
# The entry count stands in for all three numbers, since neither the part
# index nor the part count can exceed it -- so this is an upper bound.
header_bytes=$(printf \
    '# bundle format v2 (merge.sh) -- expand with split.sh\n# bundle entries: %s\n# bundle part: %s of %s\n' \
    "$entries" "$entries" "$entries" | wc -c)
header_bytes=$((header_bytes))

plan_abs="$tmp_abs.plan"
trap 'rm -f "$tmp_abs" "$body_abs" "$plan_abs"' EXIT

# One line per part: "first-line last-line entry-count byte-count".
awk -v max="$max_size" -v head="$header_bytes" '
    BEGIN { start = 1; bytes = 0; count = 0; sec_bytes = 0; sec_lines = 0 }
    {
        sec_bytes += length($0) + 1
        sec_lines += 1
        if ($0 != "===END===") next
        # A section bigger than the limit on its own still has to go
        # somewhere; it gets a part to itself rather than being cut.
        if (count > 0 && bytes + sec_bytes + head > max) {
            printf "%d %d %d %d\n", start, NR - sec_lines, count, bytes
            start = NR - sec_lines + 1
            bytes = 0; count = 0
        }
        bytes += sec_bytes
        count += 1
        sec_bytes = 0; sec_lines = 0
    }
    END {
        if (count > 0 || NR == 0)
            printf "%d %d %d %d\n", start, NR, count, bytes
    }
' "$body_abs" > "$plan_abs"

parts=$(wc -l < "$plan_abs")
parts=$((parts))
if [ "$parts" -le 1 ]; then
    # It fits: write the ordinary single bundle under the name asked for.
    {
        printf '# bundle format v2 (merge.sh) -- expand with split.sh\n'
        printf '# bundle entries: %s\n' "$entries"
        cat "$body_abs"
    } > "$tmp_abs"
    mv "$tmp_abs" "$out_abs"
    rm -f "$body_abs" "$plan_abs"
    trap - EXIT
    echo "Wrote $out: $files file(s) ($b64s base64), $dirs empty dir(s), \
$links symlink(s), $skipped skipped, $(($(wc -l < "$out_abs"))) lines"
    exit 0
fi

index=0
oversize=0
while read -r first last count bytes; do
    index=$((index + 1))
    name=$(part_name "$index" "$parts")
    piece="$tmp_abs.part"
    {
        printf '# bundle format v2 (merge.sh) -- expand with split.sh\n'
        printf '# bundle entries: %s\n' "$count"
        printf '# bundle part: %s of %s\n' "$index" "$parts"
        sed -n "${first},${last}p" "$body_abs"
    } > "$piece"
    mv "$piece" "$(abspath "$name")"
    size=$(wc -c < "$(abspath "$name")")
    size=$((size))
    [ "$size" -gt "$max_size" ] && oversize=$((oversize + 1))
    echo "Wrote $name: $count entr(y/ies), $size bytes"
done < "$plan_abs"

rm -f "$body_abs" "$plan_abs"
trap - EXIT

echo "Split $files file(s) ($b64s base64), $dirs empty dir(s), \
$links symlink(s), $skipped skipped into $parts part(s)"
if [ "$oversize" -gt 0 ]; then
    echo "merge.sh: $oversize part(s) exceed the limit: a single entry is \
larger than $max_size bytes and cannot be divided" >&2
fi
echo "Expand them into one directory, in any order:"
echo "  for p in $(part_name 1 "$parts" | sed "s/part1-of$parts/part*-of$parts/"); \
do ./split.sh \"\$p\" DEST; done"
