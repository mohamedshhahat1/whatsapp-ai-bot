# Knowledge base

Drop the company documents the bot should answer from into this folder:

```
knowledge/
  catalog.pdf      # products, materials, finishes
  prices.pdf       # price lists, packages, m2 rates
  faq.pdf          # common customer questions
  contracts.pdf    # terms, warranty, payment schedule
```

Supported formats: `.pdf`, `.md`, `.txt`.

## Indexing

```bash
python scripts/ingest_knowledge.py            # index new/changed files
python scripts/ingest_knowledge.py --force    # re-index everything
python scripts/ingest_knowledge.py --prune    # also drop deleted files
```

Each file is hashed, so re-running only re-embeds documents that actually
changed. See `docs/RAG.md` for the full pipeline.

## Why the files are not committed

Price lists and contracts are business documents, and embedding them costs
money per token. The `.gitignore` in this folder keeps everything except this
README out of version control. Copy the files onto the server (or mount a
volume) and run the ingest command there.
