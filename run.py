from app import create_app
from app.services.incoming_watcher import start_incoming_watcher
from flask import Flask
from flask.json.provider import DefaultJSONProvider
from datetime import date, datetime
from decimal import Decimal

class ISOJSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)

app = create_app()
app.json = ISOJSONProvider(app)
print(app.url_map)

if __name__ == "__main__":
    start_incoming_watcher(app)  # watches incoming/ on background threads; stops when this process does
    app.run(debug=True, use_reloader=False,port=8000)
