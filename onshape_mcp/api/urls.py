"""Parse Onshape document URLs into their IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_URL_RE = re.compile(
    r"/documents/(?P<did>[0-9a-f]{24})"
    r"(?:/(?P<wvm>[wvm])/(?P<wvmid>[0-9a-f]{24}))?"
    r"(?:/e/(?P<eid>[0-9a-f]{24}))?",
    re.IGNORECASE,
)


@dataclass
class OnshapeRef:
    """IDs extracted from an Onshape URL."""

    document_id: str
    wvm: Optional[str] = None  # "w" (workspace), "v" (version) or "m" (microversion)
    wvm_id: Optional[str] = None
    element_id: Optional[str] = None

    @property
    def workspace_id(self) -> Optional[str]:
        return self.wvm_id if self.wvm == "w" else None

    @property
    def version_id(self) -> Optional[str]:
        return self.wvm_id if self.wvm == "v" else None


def parse_onshape_url(url: str) -> OnshapeRef:
    """Extract document / workspace|version / element IDs from an Onshape URL.

    Example:
        https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>
    """
    match = _URL_RE.search(url or "")
    if not match:
        raise ValueError(
            "Not a valid Onshape document URL. Expected something like "
            "https://cad.onshape.com/documents/<documentId>/w/<workspaceId>/e/<elementId>"
        )
    wvm = match.group("wvm")
    return OnshapeRef(
        document_id=match.group("did").lower(),
        wvm=wvm.lower() if wvm else None,
        wvm_id=match.group("wvmid").lower() if match.group("wvmid") else None,
        element_id=match.group("eid").lower() if match.group("eid") else None,
    )
