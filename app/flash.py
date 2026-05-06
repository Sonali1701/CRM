from fastapi import Request
from fastapi.responses import RedirectResponse


def flash(response: RedirectResponse, message: str, type_: str = "success") -> RedirectResponse:
    response.set_cookie("flash_message", message, max_age=10, httponly=True, samesite="lax")
    response.set_cookie("flash_type", type_, max_age=10, httponly=True, samesite="lax")
    return response


def get_flash(request: Request) -> dict | None:
    msg = request.cookies.get("flash_message")
    if not msg:
        return None
    return {"message": msg, "type": request.cookies.get("flash_type", "success")}


def clear_flash_response(response) -> None:
    response.delete_cookie("flash_message")
    response.delete_cookie("flash_type")
