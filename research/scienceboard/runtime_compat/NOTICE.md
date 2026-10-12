# ScienceBoard host runtime compatibility notice

This directory provides a narrow, independently maintained compatibility
surface for ScienceBoard's pinned VMware integration. It is not the upstream
`desktop-env` package and must not be used as a general OSWorld environment.

The interface and behavior were checked against `desktop-env` 0.1.22, Copyright
the OSWorld/desktop-env contributors, distributed under the Apache License 2.0.
The exact upstream wheel and reference-file hashes are recorded in
`provenance.json`. The implementation here removes unrelated cloud, model,
training, document, and media dependency graphs; it retains only the VMware
lifecycle and guest-controller calls exercised by the pinned ScienceBoard
`VManager`.

Formal use is fail-closed: the host doctor verifies this directory's hashes,
constructor/method signatures, the pinned ScienceBoard sources, and the small
host dependency set before a VM can start.
