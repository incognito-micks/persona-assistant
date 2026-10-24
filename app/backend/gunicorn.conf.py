max_requests = 1000
max_requests_jitter = 50
log_file = "-"
bind = "0.0.0.0"

timeout = 230
# https://learn.microsoft.com/troubleshoot/azure/app-service/web-apps-performance-faqs#why-does-my-request-time-out-after-230-seconds

# Container hosts can report the node CPU count rather than the app's small CPU allocation.
workers = 1
graceful_timeout = 310
control_socket_disable = True
worker_class = "custom_uvicorn_worker.CustomUvicornWorker"
