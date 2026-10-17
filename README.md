# Acme Appliances Order Ledger Q&A

## Local setup

1. Install Python 3.10+.
2. Install dependencies: `pip install -r requirements.txt`
3. Set `API_ROOT` to the complete root URL supplied by the assignment (the URL that returns the service and links to orders/rates/export).
4. Run: `uvicorn main:app --host 0.0.0.0 --port 8000`
5. Test:
   `curl -X POST http://127.0.0.1:8000/answer -H "Content-Type: application/json" -d '{"question":"What is total paid revenue in USD?"}'`

The service follows the ledger notes: Asia/Kolkata business dates, latest `updated_at` record per order ID, and only `paid` orders count as revenue. It fetches all pages and caches the data for five minutes.

## Deploy to Render

1. Push these files to a GitHub repository.
2. In Render, create a **New Web Service** and connect the repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add environment variable `API_ROOT` with the full root URL from the assignment. Do not put it in source code or commit it.
6. Deploy. Test the public URL with `POST https://YOUR-SERVICE.onrender.com/answer` and JSON body `{"question":"What is total paid revenue in USD?"}`.

The root GET URL is a health check. POST requests are accepted at both `/` and `/answer`.
