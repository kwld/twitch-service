from app.routes.admin_routes import register_admin_routes
from app.routes.service_routes import register_service_routes
from app.routes.twitch_routes import register_twitch_routes

__all__ = [
    "register_admin_routes",
    "register_service_routes",
    "register_twitch_routes",
]
