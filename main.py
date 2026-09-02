from nicegui import app, ui

# Create a clean header and a welcoming text label
ui.label('Hello, World!').classes('text-2xl m-4 font-semibold text-primary')

if __name__ in {"__main__", "__mp_main__"}:
    # Start the built-in Uvicorn ASGI server with hot-reloading enabled
    ui.run(reload=True)
