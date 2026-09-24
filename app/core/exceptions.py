from fastapi import HTTPException, status


class AppBaseException(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


class InvalidUrlException(AppBaseException):
    def __init__(self, detail: str = "Invalid or unsupported Bilibili URL."):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class StreamNotFoundException(AppBaseException):
    def __init__(self, detail: str = "No playable video or audio streams found for this content."):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class RateLimitExceededException(AppBaseException):
    def __init__(self, detail: str = "Rate limit exceeded. Please try again later."):
        super().__init__(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


class BilibiliApiException(AppBaseException):
    def __init__(self, detail: str = "Failed to communicate with Bilibili upstream servers."):
        super().__init__(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
