"""PyInstaller entry point. A real package entry (not the module file itself)
so that the package's relative imports work when frozen."""
import sys

from cold_archive.gui.app import main

if __name__ == "__main__":
    import os
    if os.path.basename(sys.argv[0]).endswith("cold-archive-cli"):
        from cold_archive.engine.cli import main
        sys.exit(main())
    else:
        from cold_archive.gui.app import main
        sys.exit(main())
