from authlib.integrations.flask_client import OAuth


oauth = OAuth()


def init_oauth(app):
    oauth.init_app(app)

    oauth.register(
        name="google",
        client_id=app.config.get("GOOGLE_CLIENT_ID"),
        client_secret=app.config.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile https://www.googleapis.com/auth/calendar"},
    )

    oauth.register(
        name="microsoft",
        client_id=app.config.get("MS_CLIENT_ID"),
        client_secret=app.config.get("MS_CLIENT_SECRET"),
        server_metadata_url="https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile offline_access User.Read Calendars.ReadWrite"},
    )
