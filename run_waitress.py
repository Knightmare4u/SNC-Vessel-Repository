from waitress import serve
from oakmaritime.wsgi import application
import os
from decouple import config

if __name__ == '__main__':
    port = config('PORT', default=8000, cast=int)
    host = config('HOST', default='0.0.0.0')
    
    print(f"Starting SNC Vessels File Manager on {host}:{port}")
    print("Press Ctrl+C to stop the server")
    
    serve(application, host=host, port=port)