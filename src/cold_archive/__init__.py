"""Cold Archive: long-term integrity protection for archived files.

An archive protected by this tool is a plain directory tree plus:

* ``SHA256SUMS`` at the root, in the universal ``sha256sum -c`` text format,
  listing every payload file;
* ``cold-archive-manifest.json`` at the root with sizes, modification times and
  hashes, plus the settings that were used;
* ``READ-ME-FIRST.txt`` / ``.html`` recovery instructions for whoever opens the
  archive years from now, and a double-clickable ``Check-This-Archive.command``;
* per-directory PAR2 parity sets (``PARITY.par2`` and volumes) created with
  par2cmdline, which can rebuild damaged or missing files in that directory;
* a ``PARITY-INDEX.par2`` set at the root protecting the checksum list, the
  manifest and the instructions at a higher redundancy.

Nothing is compressed, encrypted or packed: every file stays a normal file so
that the archive remains readable with no software at all, and repairable with
any PAR2 implementation.
"""

__version__ = "0.3.0"
APP_NAME = "Cold Archive"
BUNDLE_ID = "nyc.billie.coldarchive"
