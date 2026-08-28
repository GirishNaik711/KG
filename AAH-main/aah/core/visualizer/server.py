#!/usr/bin/env python3
"""
RAPIDS Visualizer Server

Launches HTTP server to visualize RAPIDS artifacts.

Usage:
    aah run core.visualizer.server
    aah run core.visualizer.server --workspace ws01 --project my-project
    aah run core.visualizer.server --port 9000

The server runs from the project directory and serves:
- Frontend HTML/JS files from visualizer/html/
- Project artifacts (.rapids/, knowledge/) from project directory
- Dynamic rapids-config.json generated in-memory
"""

import argparse
import json
import os
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


class RAPIDSHTTPHandler(SimpleHTTPRequestHandler):
    """
    Hybrid HTTP handler:
    - Generates /rapids-config.json dynamically
    - Serves frontend files from visualizer/html/
    - Falls back to project directory for artifacts
    """

    visualizer_html_dir = None
    project_name = None

    # Directory naming convention fallbacks
    # Format: (original_pattern, fallback_pattern)
    PATH_FALLBACKS = [
        ('/.knowledge/', '/knowledge/'),
        ('/knowledge/', '/.knowledge/'),
        # Add more fallbacks as needed:
        # ('/.config/', '/config/'),
        # ('/docs/', '/documentation/'),
    ]

    def do_GET(self):
        """Handle GET requests with fallback logic"""

        # Special case: dynamic config generation
        if self.path == '/rapids-config.json':
            self.serve_dynamic_config()
            return

        # Try visualizer/html/ first
        requested_file = self.path.lstrip('/') if self.path != '/' else 'index.html'
        visualizer_file = Path(self.visualizer_html_dir) / requested_file

        if visualizer_file.exists() and visualizer_file.is_file():
            self.serve_file_from_path(visualizer_file)
            return

        # Try directory naming fallbacks
        alternate_path = self.try_path_fallbacks(self.path)
        if alternate_path:
            original_path = self.path
            self.path = alternate_path
            super().do_GET()
            self.path = original_path
            return

        # Fall back to project directory (default behavior)
        super().do_GET()

    def try_path_fallbacks(self, original_path: str) -> str | None:
        """
        Try alternative directory naming conventions.

        Returns alternate path if a fallback file exists, otherwise None.
        """
        for pattern, replacement in self.PATH_FALLBACKS:
            if pattern in original_path:
                alternate_path = original_path.replace(pattern, replacement, 1)
                alternate_file = Path('.') / alternate_path.lstrip('/')

                if alternate_file.exists() and alternate_file.is_file():
                    return alternate_path

        return None

    def serve_file_from_path(self, filepath: Path):
        """Serve file from given path"""
        try:
            self.send_response(200)
            content_type = self.guess_type(str(filepath))
            self.send_header('Content-type', content_type)
            self.send_header('Content-Length', filepath.stat().st_size)
            self.end_headers()

            with open(filepath, 'rb') as f:
                self.copyfile(f, self.wfile)
        except Exception as e:
            self.send_error(500, f"Error serving file: {e}")

    def serve_dynamic_config(self):
        """Generate and serve rapids-config.json in-memory"""
        config = generate_config(self.project_name)
        config_json = json.dumps(config, indent=2).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Content-Length', len(config_json))
        self.end_headers()
        self.wfile.write(config_json)

    def guess_type(self, path):
        """Extended MIME type detection for RAPIDS files"""
        base_type = super().guess_type(path)

        # Add RAPIDS-specific types
        if path.endswith('.yaml') or path.endswith('.yml'):
            return 'text/yaml'
        elif path.endswith('.mmd'):
            return 'text/plain'
        elif path.endswith('.md'):
            return 'text/markdown'

        return base_type

    def end_headers(self):
        """Override to add no-cache headers to all responses"""
        # Add no-cache headers for all responses (development mode)
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def log_message(self, format, *args):
        """Suppress request logging (optional - keeps output clean)"""
        # Uncomment to see request logs:
        # super().log_message(format, *args)
        pass


def generate_config(project_name: str) -> dict:
    """
    Generate rapids-config.json dynamically

    All paths use absolute URLs (starting with /) to ensure they resolve
    correctly from any page location (e.g., /utilities/dag-viewer.html)

    Note: Server has fallback logic for .knowledge → knowledge
    (if .knowledge/file.md doesn't exist, tries knowledge/file.md)
    """
    return {
        "$schema": "/rapids-config.schema.json",
        "version": "1.0.0",
        "description": f"RAPIDS Visualizer Config for {project_name}",

        "project": {
            "name": project_name,
            "workspace_root": ".",
            "active_workspace": ".",
            "active_project": "."
        },

        "paths": {
            "dag": {
                "file": "/.rapids/plan/dag.json",
                "auto_load": True
            },
            "waves": {
                "file": "/.rapids/plan/waves.json",
                "auto_load": True
            },
            "features": {
                "file": "/.rapids/plan/feature-list.json",
                "auto_load": False
            },
            "manifest": {
                "file": "/.rapids/manifest.yaml",
                "auto_load": True
            },
            "intake": {
                "file": "/.rapids/intake.json",
                "auto_load": True
            },
            "project_brief": {
                "file": "/.knowledge/project-brief.md",
                "auto_load": True
            },
            "phase_plan": {
                "file": "/.rapids/phase-plan.yaml",
                "auto_load": True
            },
            "progress": {
                "file": "/.rapids/claude-progress.json",
                "auto_load": True
            },
            "iteration_history": {
                "file": "/.rapids/iteration-history.yaml",
                "auto_load": True
            },
            "checkpoint_results": {
                "dir": "/.rapids/implement/checkpoint-results",
                "auto_load": False
            },
            "feedback": {
                "system": "/.rapids/implement/feedback/system-feedback-wave-{wave}.json",
                "user": "/.rapids/implement/feedback/user-feedback-wave-{wave}.json",
                "auto_load": False
            },
            "test_results": {
                "dir": "/.rapids/implement/test-results",
                "auto_load": False
            },
            "runtime_results": {
                "dir": "/.rapids/implement/runtime-results",
                "auto_load": False
            },
            "knowledge_base": {
                "project_brief": "/.knowledge/project-brief.md",
                "domain_brief": "/.knowledge/domain-brief.md"
            },
            "subagent_learnings": {
                "file": "/.rapids/implement/subagent-learnings.json",
                "auto_load": True
            },
            "brownfield": {
                "codebase_learning": "/.rapids/codebase-intel/codebase-learning.md",
                "codebase_profile": "/.rapids/codebase-intel/codebase-profile.json",
                "architecture_mmd": "/.rapids/codebase-intel/architecture.mmd",
                "architecture_md": "/.rapids/codebase-intel/architecture-diagram.md",
                "data_model_md": "/.rapids/codebase-intel/data-model-diagram.md",
                "data_flow_md": "/.rapids/codebase-intel/data-flow-diagram.md",
                "dependency_graph_md": "/.rapids/codebase-intel/dependency-graph.md",
                "codebase_structure": "/.rapids/codebase-intel/codebase-structure.md",
                "tech_stack": "/.rapids/codebase-intel/tech-stack.md",
                "dependency_map": "/.rapids/codebase-intel/dependency-map.md",
                "auto_load": False
            }
        },

        "ui": {
            "theme": "light",
            "auto_layout": True,
            "show_upload_gate": False,
            "enable_local_storage": True
        },

        "features": {
            "dag_viewer": {
                "enabled": True,
                "default_view": "all_waves"
            },
            "checkpoint_dashboard": {
                "enabled": False
            },
            "feedback_manager": {
                "enabled": False
            }
        }
    }


def parse_args():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(
        description='RAPIDS Visualizer Server - Web-based RAPIDS artifact viewer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aah run core.visualizer.server
  aah run core.visualizer.server --workspace ws01 --project my-project
  aah run core.visualizer.server --port 9000
        """
    )
    parser.add_argument('--project-path', help='Project folder (default: walk up from cwd for .rapids/)')
    parser.add_argument('--port', type=int, default=8000, help='Server port (default: 8000)')
    return parser.parse_args()


def resolve_project_path(project_path: str = None) -> Path:
    """Resolve the project folder (the one holding ``.rapids/``).

    Uses an explicit ``--project-path`` if given, else walks up from cwd —
    the folder=project model. No workspace, no config file.
    """
    from aah.core.common.config import resolve_project_path as _resolve

    explicit = Path(project_path) if project_path else None
    resolved = _resolve(explicit)
    if resolved is None:
        raise FileNotFoundError(
            "No RAPIDS project found (no .rapids/manifest.yaml from cwd upward).\n"
            "  cd to your project folder, or pass --project-path <path>."
        )
    return resolved.resolve()


def print_server_info(project_path: Path, port: int):
    """Print server startup information"""
    print("\n🚀 RAPIDS Visualizer Server")
    print("━" * 60)
    print(f"Project:   {project_path.name}")
    print(f"Location:  {project_path}")
    print(f"Port:      {port}")
    print("━" * 60)
    print(f"\n✅ Server running at http://localhost:{port}\n")
    print(f"📊 Main Dashboard:     http://localhost:{port}/")
    print(f"🗺️  DAG Viewer:        http://localhost:{port}/utilities/dag-viewer.html")
    print(f"🏗️  Brownfield Viewer: http://localhost:{port}/utilities/brownfield-viewer.html")
    print(f"📚 Knowledge Base:     http://localhost:{port}/utilities/knowledge-base-viewer.html")
    print(f"\nServing files from: {project_path}")
    print("Press Ctrl+C to stop\n")


def main():
    """Main entry point"""
    try:
        # Parse arguments
        args = parse_args()

        # Find visualizer html directory (sibling to this file)
        visualizer_html_dir = Path(__file__).parent / 'html'

        if not visualizer_html_dir.exists():
            print(f"❌ Error: Visualizer HTML directory not found: {visualizer_html_dir}", file=sys.stderr)
            print("   Expected location: aah/core/visualizer/html/", file=sys.stderr)
            sys.exit(1)

        # Resolve project path
        project_path = resolve_project_path(args.project_path)

        # Change to project directory (becomes document root)
        os.chdir(project_path)

        # Configure custom handler
        RAPIDSHTTPHandler.visualizer_html_dir = visualizer_html_dir
        RAPIDSHTTPHandler.project_name = project_path.name

        # Create HTTP server
        server = HTTPServer(('localhost', args.port), RAPIDSHTTPHandler)

        # Print info and open browser
        print_server_info(project_path, args.port)
        webbrowser.open(f'http://localhost:{args.port}', encoding='utf-8')

        # Serve forever
        server.serve_forever()

    except KeyboardInterrupt:
        print("\n\n👋 Server stopped")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
