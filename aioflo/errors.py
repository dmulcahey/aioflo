"""Define package errors."""


class FloError(Exception):
    """Define a base error."""

    pass


class RequestError(FloError):
    """Define an error related to invalid requests."""

    pass


class CredentialsRejectedError(RequestError):
    """Define an error related to rejected login credentials."""

    pass
