"""Vercel Serverless Function entrypoint for the FastAPI application."""

from x_tracker.server import create_app

app = create_app()
