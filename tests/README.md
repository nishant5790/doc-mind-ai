# Component test scripts

Plain Python equivalents of the notebooks in `notebooks/`. Each script
exercises a single component end-to-end so you can sanity-check the
configuration without opening Jupyter.

Run from the repo root with the project venv activated:

```powershell
python tests/test_01_blob_storage.py
python tests/test_02_doc_intelligence.py
python tests/test_03_openai_vision.py
python tests/test_04_ai_search.py
python tests/test_05_cosmos_db.py
python tests/test_06_ingestion_pipeline.py
python tests/test_07_rag_query.py
python tests/test_08_self_improvement.py
```

Some tests need a sample file dropped next to them (or in `notebooks/`):

| Script | Required asset |
| --- | --- |
| `test_02_doc_intelligence.py` | `notebooks/Multi_Agent_Research_System_Architecture.pdf` (or `tests/sample.pdf`) |
| `test_03_openai_vision.py` | `tests/sample.png` (or `notebooks/sample.png`) |
| `test_06_ingestion_pipeline.py` | same PDF as #2 |

Tests 7 and 8 assume the index already contains data ingested by test 6.
