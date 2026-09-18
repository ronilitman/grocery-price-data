# Product name review (KAN-29)

Candidate rows for `data/product_names.tsv`, proposed by
`scripts/propose_names.py` for a human to review — not a source of truth,
and no row here has been applied to `data/product_names.tsv`.

- Chain databases: GitHub Actions run 35318063298 of `build.yml`
  (succeeded 2026-09-18T07:09Z), one `db-<CHAIN>` artifact per chain (33
  per-chain SQLite DBs), read-only.
- Generated with:
  `python3 scripts/propose_names.py --chain-dbs chain_dbs --out review/product_names_review.tsv`
  (full run, no `--barcodes` filter — every barcode in the chain DBs).
- `current_name` column added after generation: the name the app shows
  today for that barcode, read from `products.name` in
  `/Users/ronilitman/Documents/worktrees/kan-6-scratch/prices.db` — run
  35318063298 published no merged `app.db`/`prices.db` artifact, so this
  is still the 2026-09-13 database, older than the proposals in this
  review. Empty where the barcode is not in that table.
- Columns: `barcode, current_name, proposed_name, source, brand, chains, candidates`.
- Sorted so the rows most likely to need attention come first: any
  `tie-longest` source first, then `capping-fallback`, then the rest; most
  chains first within each group. The split below preserves this order —
  part 01 is the highest-priority reading.

## Parts

One logical file split into 2 numbered parts (each with its own header) to
stay under GitHub's file-size warning threshold:

- `product_names_review.01.tsv`
- `product_names_review.02.tsv`

Accepted rows get copied into `data/product_names.tsv` by hand.
