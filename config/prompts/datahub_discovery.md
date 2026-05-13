# DataHub Discovery — Analysis Prompt

You have been given search results and entity metadata from DataHub.

Analyse the results and produce a report covering:

1. **Candidate data products** — entities with high downstream usage, complete documentation,
   and ownership. List the top 5 with URN, owner, and a one-line description.
2. **Candidate golden reports** — dashboards or datasets that are widely used, stable,
   and have clear business ownership. List the top 5.
3. **Duplicate dashboards** — pairs of dashboards with similar names, identical datasets,
   and overlapping chart types. Flag any pair with similarity >80%.
4. **Governance gaps** — entities missing description, owner, or domain tags.
5. **Recommended actions** — prioritised list of governance improvements.
