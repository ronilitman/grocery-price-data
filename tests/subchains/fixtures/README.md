# Sub-chain fixtures

One directory per scenario, each holding a real dump carved down to what is
under test, with its header (`ChainID`, `SubChainID`, `StoreID`) intact and the
chain's own filename kept — so `read_offers()` walks these exactly as it walks
a production dumps directory.

**Nothing here is hand-written**, for the same reason as the promotion
fixtures: the bug being guarded against is "the file did not mean what we
assumed", and an invented file would encode the assumption instead.

| directory | what it holds | why it is here |
|---|---|---|
| `netiv_hesed_shira_branch` | two promotions from שירה מרקט גבעת שמואל, published 8 Sep 2026 | the filename says `-009-` and the document inside says `<SubChainID>000</SubChainID>` — believe the document and every promotion in the chain lands on a fourth, nameless chain |

## Refreshing one

The portal keeps roughly a day of files, so these cannot be re-downloaded
later. Carve a new one with its header rather than editing a file by hand; see
`tests/promotions/fixtures/README.md` for the snippet.
