# For the BaoTa (aaPanel) Python project. Binds loopback; the public internet
# reaches it through Nginx /api/trip/.
bind = "127.0.0.1:5003"
user = "www"
workers = 1
threads = 1
backlog = 512
chdir = "/www/wwwroot/trip_api/backend"
worker_class = "uvicorn.workers.UvicornWorker"
access_log_format = '%(t)s %(p)s %(h)s "%(r)s" %(s)s %(L)s %(b)s %(f)s" "%(a)s"'
loglevel = "info"
errorlog = chdir + "/logs/error.log"
accesslog = chdir + "/logs/access.log"
pidfile = chdir + "/logs/trip_api.pid"
