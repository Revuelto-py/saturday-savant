web: gunicorn main:app --workers 2 --threads 6 --worker-class gthread --timeout 60 --graceful-timeout 30 --max-requests 2000 --max-requests-jitter 200
