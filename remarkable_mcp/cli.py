#!/usr/bin/env python3
"""
CLI entry point for reMarkable MCP Server.

Usage:
    # As MCP server (default, uses cloud API)
    remarkable-mcp

    # Use SSH transport (direct connection via USB)
    remarkable-mcp --ssh

    # Convert one-time code to token (run once)
    remarkable-mcp --register <one-time-code>

    # Fetch one notebook by path and emit JSON (for batch consumers)
    remarkable-mcp --fetch-notebook "/Notes"
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main():
    """Main entry point - handle CLI args or run MCP server."""
    parser = argparse.ArgumentParser(
        description="reMarkable MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register and get token (run once)
  uvx remarkable-mcp --register abcd1234

  # Run as MCP server (cloud API)
  uvx remarkable-mcp

  # Run with token from environment
  REMARKABLE_TOKEN="your-token" uvx remarkable-mcp

  # Run with USB web interface
  uvx remarkable-mcp --usb

  # Run with SSH transport (direct USB connection, requires dev mode)
  uvx remarkable-mcp --ssh

  # SSH with custom host (e.g., using SSH config)
  REMARKABLE_SSH_HOST="remarkable" uvx remarkable-mcp --ssh

USB Web Interface Environment Variables:
  REMARKABLE_USB_HOST      USB web interface host (default: http://10.11.99.1)
  REMARKABLE_USB_TIMEOUT   Request timeout in seconds (default: 10)

SSH Environment Variables:
  REMARKABLE_SSH_HOST      SSH host (default: 10.11.99.1 for USB)
  REMARKABLE_SSH_USER      SSH user (default: root)
  REMARKABLE_SSH_PORT      SSH port (default: 22)
  REMARKABLE_SSH_PASSWORD  SSH password (optional, requires sshpass)

Security Note:
  For better security, set up SSH key authentication instead of using
  a password. See: https://github.com/SamMorrowDrums/remarkable-mcp/blob/main/docs/ssh-setup.md
""",
    )
    parser.add_argument(
        "--register",
        metavar="CODE",
        help="Register with reMarkable using a one-time code and print the token",
    )
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="Use SSH transport instead of cloud API (requires developer mode)",
    )
    parser.add_argument(
        "--usb",
        action="store_true",
        help="Use USB web interface (connect via USB cable, enable in Storage Settings)",
    )
    parser.add_argument(
        "--fetch-notebook",
        metavar="PATH",
        help=(
            "Fetch one notebook by path (e.g. '/Notes') and emit JSON on "
            "stdout with page_ids, per-page OCR text, and the OCR backend "
            "used. Intended for batch consumers like the LifeOS "
            "remarkable-poll worker. Honors the same transport and OCR "
            "env vars as the MCP server."
        ),
    )

    args = parser.parse_args()

    if args.fetch_notebook:
        # Batch fetch mode — emit JSON for one notebook and exit.
        try:
            _run_fetch_notebook(args.fetch_notebook)
        except FetchNotebookError as exc:
            print(json.dumps({"error": str(exc), "type": exc.error_type}), file=sys.stderr)
            sys.exit(exc.exit_code)
        except Exception as exc:  # pragma: no cover — defensive
            print(json.dumps({"error": str(exc), "type": "unexpected"}), file=sys.stderr)
            sys.exit(1)
        return

    if args.register:
        # Registration mode - convert one-time code to token
        # Only import what's needed for registration
        from remarkable_mcp.api import register_and_get_token

        try:
            print(f"Registering with reMarkable using code: {args.register}")
            token = register_and_get_token(args.register)
            print("\n✅ Successfully registered!\n")
            print("Your token (add to mcp.json env):")
            print("-" * 50)
            print(token)
            print("-" * 50)
            print("\nAdd to your .vscode/mcp.json:")
            print(
                json.dumps(
                    {
                        "servers": {
                            "remarkable": {
                                "command": "uvx",
                                "args": ["remarkable-mcp"],
                                "env": {"REMARKABLE_TOKEN": token},
                            }
                        }
                    },
                    indent=2,
                )
            )
        except Exception as e:
            print(f"❌ Registration failed: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.usb:
        # USB web mode - set environment variable and run server
        os.environ["REMARKABLE_USE_USB_WEB"] = "1"
        from remarkable_mcp.server import run

        run()
    elif args.ssh:
        # SSH mode - set environment variable and run server
        os.environ["REMARKABLE_USE_SSH"] = "1"
        from remarkable_mcp.server import run

        run()
    else:
        # MCP server mode - only now import the full server
        from remarkable_mcp.server import run

        run()


class FetchNotebookError(Exception):
    """Raised by _run_fetch_notebook on a known failure mode.

    Carries a machine-readable ``error_type`` and a non-zero ``exit_code``
    so batch consumers can branch on the reason cleanly.
    """

    def __init__(self, message: str, *, error_type: str, exit_code: int = 1):
        super().__init__(message)
        self.error_type = error_type
        self.exit_code = exit_code


def _run_fetch_notebook(notebook_path: str) -> None:
    """Fetch one notebook by path, run OCR, and print JSON to stdout.

    Output shape (success):
        {
          "notebook_id":   "<rM doc UUID>",
          "notebook_path": "/Notes",
          "pages":         N,
          "page_ids":      ["uuid", ...],
          "ocr_text":      ["text per page or empty string", ...],
          "ocr_backend":   "openrouter" | "google" | "xai" | "tesseract" | null,
          "typed_text":    [...]  # from rmscene parsing, may be empty
        }

    Raises ``FetchNotebookError`` on known failure modes.
    """
    # Imports are deferred so the CLI's --help and --register paths don't
    # pull in the heavy stack.
    from remarkable_mcp.api import (
        get_item_path,
        get_items_by_id,
        get_meta_items_cached,
        get_rmapi,
    )
    from remarkable_mcp.extract import extract_text_from_document_zip

    try:
        client = get_rmapi()
    except Exception as exc:
        raise FetchNotebookError(
            f"Failed to initialise reMarkable client: {exc}",
            error_type="client_init_failed",
        )

    try:
        collection = get_meta_items_cached(client)
    except Exception as exc:
        raise FetchNotebookError(
            f"Failed to list documents: {exc}",
            error_type="list_failed",
        )

    items_by_id = get_items_by_id(collection)
    documents = [item for item in collection if not item.is_folder]

    # Match by name (case-insensitive) or by full path (case-insensitive).
    needle = notebook_path.lower().strip("/")
    target_doc = None
    for doc in documents:
        if doc.VissibleName.lower() == needle:
            target_doc = doc
            break
        if get_item_path(doc, items_by_id).lower().strip("/") == needle:
            target_doc = doc
            break

    if target_doc is None:
        raise FetchNotebookError(
            f"Notebook not found: {notebook_path!r}",
            error_type="not_found",
            exit_code=2,
        )

    resolved_path = get_item_path(target_doc, items_by_id)

    try:
        raw_zip = client.download(target_doc)
    except Exception as exc:
        raise FetchNotebookError(
            f"Failed to download notebook: {exc}",
            error_type="download_failed",
        )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(raw_zip)
            tmp_path = Path(tmp.name)
        try:
            content = extract_text_from_document_zip(
                tmp_path,
                include_ocr=True,
                doc_id=target_doc.ID,
            )
        except Exception as exc:
            raise FetchNotebookError(
                f"Failed to extract content: {exc}",
                error_type="extract_failed",
            )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    handwritten = content.get("handwritten_text") or []
    page_ids = content.get("page_ids") or []
    pages = content.get("pages") or len(page_ids)

    # Normalise handwritten_text to length == pages so the consumer can
    # index by page_index reliably even if OCR returned empty for some
    # pages.
    if len(handwritten) < pages:
        handwritten = list(handwritten) + [""] * (pages - len(handwritten))

    payload = {
        "notebook_id": target_doc.ID,
        "notebook_path": resolved_path,
        "pages": pages,
        "page_ids": page_ids,
        "ocr_text": handwritten,
        "ocr_backend": content.get("ocr_backend"),
        "typed_text": content.get("typed_text") or [],
    }

    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
