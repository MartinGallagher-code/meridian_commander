# Data tools tutorial

Meridian Commander ships three plug-ins for working with tabular data —
**Profile table**, **Clean table** and **Build dataset** — covering data
analysis, cleaning and dataset creation for CSV / TSV / JSON-lines files. They
are pure standard library and read through the same filesystem layer as the rest
of the app, so they work on local **and** remote (SFTP/SSH/FTP) panes.

## The mental model (read this first)

Meridian Commander has two panes. A data tool is a **pane plug-in**: it takes
over the pane you open it in, and operates on the file **highlighted (or tagged)
in the opposite pane** — exactly like the built-in "Find in other pane".

```
┌── LEFT pane ─────────────┐   ┌── RIGHT pane ────────────┐
│  ~/data                  │   │  [plugin] Profile table  │
│  > sales.csv     ← target│   │  profile>                │
│    customers.csv         │   │  (you type commands here)│
└──────────────────────────┘   └──────────────────────────┘
```

So the routine is always:

1. In one pane, navigate to and highlight your file (or press **Space** to tag
   several).
2. Switch to the other pane (**Tab**), press **`p`** (or **F11**), and pick the
   tool from the menu.
3. Type commands at the prompt; **Enter** runs, **Esc** closes.

Output files are written into the *data* pane's directory, and that pane
refreshes so the results appear.

**Keys inside a tool:** `Enter` run · `Esc` close · `PgUp`/`PgDn` scroll output ·
`↑`/`↓` command history · `Tab` switch panes.

## 1. Profile table — understand a file

Select a CSV in the other pane and open **Profile table** (`profile>`).

| Command | What it does |
| --- | --- |
| *(just press Enter)* | Full profile: shape + every column |
| `col <name>` | Drill into one column (histogram or value counts) |
| `head [n]` / `tail [n]` | Preview first / last rows (default 20) |

**Empty command → full profile:**

```
profile>
sales.csv: 4 row(s) x 2 column(s)  (delimiter ';')
  region [str]  nulls=0 (0%)  distinct=2
    top: 'north'x2, 'south'x2
  amount [int]  nulls=1 (25%)  distinct=3
    min=5 max=20 mean=11.666667 median=10
```

Note that it **auto-detected the `;` delimiter**, inferred each column's type,
counted nulls, and gave numeric statistics.

**Drill into a numeric column → histogram:**

```
profile> col amount
  amount [int]  nulls=1 (25%)  distinct=3
    min=5 max=20 mean=11.666667 median=10
    distribution:
             5 | ######## 1
            10 | ######## 1
            20 | ######## 1
```

**Drill into a text column → value counts:**

```
profile> col region
    value counts:
      'north': 2
      'south': 2
```

## 2. Clean table — fix a file (writes a *copy*)

Open **Clean table** (`clean>`) on the selected file. Every verb writes a **new**
file `‹name›.cleaned.csv` — the source is never modified. Prefix any command with
**`preview`** to see the result without writing.

| Verb | Example | Effect |
| --- | --- | --- |
| `trim` | `trim` | Strip surrounding whitespace from every cell |
| `dedupe [cols]` | `dedupe` or `dedupe email` | Drop duplicate rows (whole row, or by columns) |
| `dropnull <cols>` | `dropnull email` | Drop rows where a listed column is empty |
| `fillnull <col> <val>` | `fillnull region unknown` | Replace empties in a column |
| `drop <cols>` | `drop notes,temp` | Remove columns |
| `keep <cols>` | `keep id,email` | Keep only these columns |
| `rename <old> <new>` | `rename amt amount` | Rename a column |
| `filter <col> <op> <val>` | `filter amount > 100` | Keep matching rows |
| `retype <col> <int\|float>` | `retype amount int` | Validate / normalize a numeric column |
| `normalize-headers` | `normalize-headers` | snake_case the header row |

**filter operators:** `==` `!=` `>` `<` `>=` `<=` `contains` (numeric comparison
when both sides are numbers, otherwise text).

**Preview before committing:**

```
clean> preview dedupe
dedupe  (5 -> 3 rows)
  (preview -- nothing written)
  a   b
  --- ---
  1   x
  2   y
```

Drop the `preview` prefix to actually write it:

```
clean> dedupe
dedupe  (5 -> 3 rows)
  wrote t.cleaned.csv
```

`retype` doubles as a validator: if a value cannot convert, it refuses and writes
nothing, telling you how many are bad:

```
clean> retype amount int
error: 2 value(s) in 'amount' are not int; nothing written
```

> **Chaining steps.** Each run reads the *originally selected* file, so to apply
> several steps in a row, run one, then select the resulting `.cleaned.csv` and
> run the next. (Successive outputs are numbered — `t.cleaned2.csv`, etc.)

## 3. Build dataset — combine & reshape files

Open **Build dataset** (`build>`). Some verbs use **multiple tagged files** — tag
them with **Space** in the other pane first.

| Verb | Needs | Output |
| --- | --- | --- |
| `concat` | ≥1 tagged files | `dataset.csv` — stacks rows, **union of columns** (missing → empty) |
| `join <key>` | ≥2 tagged files | `joined.csv` — left-join the first onto the second on `<key>` |
| `sample <n>` or `<n%>` | 1 file | `‹name›.sample.csv` |
| `split <col>` | 1 file | one `‹name›.‹col›-‹value›.csv` per distinct value |
| `to-jsonl` | 1 CSV | `‹name›.jsonl` |
| `from-jsonl` | 1 JSONL | `‹name›.csv` |
| `groupby <col> <agg>[:col]` | 1 file | `‹name›.groupby.csv` |

**Concat (union of columns)** — tag `a.csv` (cols `x,y`) and `b.csv` (cols
`y,z`):

```
build> concat
concat 2 file(s) -> dataset.csv  (2 rows)
```

The header becomes `x,y,z`; each row fills only its known columns.

**Join two files on a key** — tag `customers.csv` and `orders.csv`:

```
build> join id
left join on 'id': customers.csv + orders.csv -> joined.csv  (18/20 rows matched)
```

**Split one file into many by a column:**

```
build> split region
split by 'region' into 2 file(s):
  sales.region-north.csv
  sales.region-south.csv
```

**Group-by aggregation** — `agg` is one of `count`, `sum`, `mean`, `min`, `max`;
all but `count` need a value column via `:col`:

```
build> groupby region sum:amount
groupby 'region' sum [stdlib] -> sales.groupby.csv  (2 groups)
```

```
region,sum_amount
north,30
south,5
```

The `[stdlib]` tag means it used the pure-Python path. If you install the
optional `meridian-commander[data]` extra, large files use pandas automatically
with identical output (shown as `[pandas]`).

**CSV ↔ JSON-lines:**

```
build> to-jsonl
to-jsonl 3 row(s) -> sales.jsonl
```

## 4. Configuration (optional)

Press **`C`** → *Edit configuration*. Each tool reads its own section:

```ini
[plugin:csv_profile]
delimiter =            ; blank = auto-detect (comma/tab/;/|); or: tab, comma, semicolon, pipe
encoding = utf-8
has_header = yes       ; set "no" for headerless files (columns become col1, col2, ...)
top_n = 5              ; most-common values shown per text column
preview_rows = 20      ; default rows for head/tail
max_bytes = 67108864   ; cap on how much of a huge file is read

[plugin:csv_clean]     ; delimiter / encoding / has_header / max_bytes
[plugin:csv_build]     ; delimiter / encoding / has_header / max_bytes
```

Everything is **bounded** — a file is read at most `max_bytes`, and a tool tells
you when it stopped early (`! source was truncated at the byte cap`), so a
multi-gigabyte CSV cannot lock up the interface.

## 5. A complete worked example

You have a messy `sales.csv` and want per-region totals from clean, deduped data:

```
1. Highlight sales.csv (left pane). Tab to right pane, p -> Profile table.
   profile>                     # see shape, spot 25% nulls in `amount`, ';' delimiter

2. Esc, p -> Clean table (still pointing at sales.csv).
   clean> dropnull amount       # -> sales.cleaned.csv

3. Highlight sales.cleaned.csv, p -> Clean table.
   clean> dedupe                # -> sales.cleaned.cleaned.csv

4. Highlight that file, p -> Build dataset.
   build> groupby region sum:amount   # -> *.groupby.csv with your totals
```

See the main [README](../README.md#plug-ins) for the plug-in system in general
and how to write your own.
