# Product selection

Search for items, read one product, then resolve its options to a purchasable variant. A quote prices a variant.

```bash
python3 scripts/register.py products --query "<search words>"
python3 scripts/register.py products --query "<search words>" --limit 10
python3 scripts/register.py product "<product-id>"
python3 scripts/register.py variant "<product-id>"
python3 scripts/register.py variant "<product-id>" --option "<name>=<value>"
```

Replace placeholders before execution. `products` prints the id, title, merchant when available, and price. `no price` means that the service has no price for that row.

Read the selected product. Each `Option <name>: <values>` line lists a choice needed to resolve the variant. Use choices the user already supplied when they match the returned options. Ask only for missing or ambiguous choices.

Repeat `--option` for each choice, for example `--option "Size=M" --option "Color=Blue"`. Give one `--option` for every printed option. Labels are case sensitive. Omit `--option` only when the product prints no options. The helper reads the current product details and sends the published option ids for the exact labels you selected. Unknown or ambiguous choices stop before variant resolution.

Selection is complete when `variant` prints an id and `Variant: purchasable`. Use that id for [a quote](quotes.md). Report the returned titles, prices, and options.

## Recovery

- `No products found.`: the service can return no products for a search that finds them when it runs again. Run the same search again, up to 2 more times. Then search with only the product name, without a brand or store name. If the search still finds nothing, ask the user for more detail.
- `Variant: not purchasable`: ask for a different option or product.
- `Option must be stated as name=value.`: repeat `--option` once for each choice.
- An invalid product response: follow [Debug](debug.md).
