from app.routes.admin_routes import register_admin_routes
from app.routes.service_routes import register_service_routes
from app.routes.system_routes import register_system_routes
from app.routes.twitch_routes import register_twitch_routes
from app.routes.ws_routes import register_ws_routes

__all__ = [
    "register_admin_routes",
    "register_service_routes",
    "register_system_routes",
    "register_twitch_routes",
    "register_ws_routes",
]
