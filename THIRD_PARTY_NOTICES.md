# Third-Party Notices

BenchSuite is a Python GTK4 application. The BenchSuite source code is licensed
under the MIT License in `LICENSE`.

This repository does not vendor third-party benchmark binaries. Most tools
below are detected at runtime or installed by the user through the operating
system package manager, Snap, Cargo, or a manual upstream build.

If you distribute a package that bundles any of these tools, include the
corresponding license text, copyright notices, and source-code offer or source
distribution when required by that tool's license.

## Sources bundled with BenchSuite

| Component | How BenchSuite Uses It | License |
|---|---|---|
| `third_party/benchsuite_oclburn/oclburn.c` | OpenCL GPU burn with result verification, compiled on first use | BenchSuite code (MIT), no third-party code |
| `third_party/chartjs/chart.umd.min.js` | Chart.js v4.4.1 embedded inline in HTML reports for offline viewing | MIT License |
| `third_party/chartjs/hammer.min.js` | Hammer.js v2.0.8 gesture dependency for Chart.js zoom plugin | MIT License |
| `third_party/chartjs/chartjs-plugin-zoom.min.js` | chartjs-plugin-zoom v2.0.1 interactive chart zoom embedded in HTML reports | MIT License |

## Python and UI Dependencies

| Component | How BenchSuite Uses It | License |
|---|---|---|
| Python | Runtime | Python Software Foundation License |
| PyGObject / python3-gi | GTK bindings | LGPL-2.1-or-later |
| GTK 4 | GUI toolkit | LGPL-2.1-or-later |
| Libadwaita | GNOME/GTK widgets | LGPL-2.1-or-later |
| Cairo / Pycairo | Drawing and GTK integration | LGPL/MPL family, depending on component |
| psutil | System metrics | BSD-3-Clause |
| ReportLab | PDF export | BSD license |

## External Hardware and Benchmark Tools

| Tool | How BenchSuite Uses It | Typical License |
|---|---|---|
| stress-ng | CPU/RAM stress tests through subprocess execution | GPL-2.0-only or GPL-2.0-or-later depending on package/version |
| fio | Storage benchmarks through subprocess execution | GPL-2.0-or-later |
| iperf3 | Network throughput tests through subprocess execution | BSD-3-Clause-style license |
| gpu-burn | Optional GPU stress test, detected or installed separately | BSD-2-Clause |
| gpu-fryer | Optional GPU stress test, detected or installed separately | Apache-2.0 |
| glmark2 | Optional/legacy GPU benchmark if available | GPL-3.0-only |
| smartmontools / smartctl | SMART storage health data | GPL-2.0-or-later |
| nvme-cli | NVMe controller and SMART data | GPL-2.0-only |
| ipmitool | BMC/IPMI sensor data | BSD-style license |
| dmidecode | DMI/SMBIOS hardware inventory | GPL-2.0-or-later |
| pciutils / lspci | PCI/PCIe hardware inventory | GPL-2.0-or-later |
| usbutils / lsusb | USB hardware inventory | GPL-2.0-or-later |
| ethtool | Network interface and driver data | GPL-2.0-only |
| lm-sensors / sensors | Temperature, voltage, fan and power data | LGPL/GPL components depending on package |
| powerstat | Optional CPU/platform power telemetry | GPL-2.0-or-later |
| nvidia-smi / NVIDIA NVML | NVIDIA GPU telemetry when NVIDIA drivers are installed | NVIDIA proprietary license |
| STREAM (`third_party/stream/stream.c`, unmodified) | Memory bandwidth benchmark (John D. McCalpin) | STREAM license: free use and redistribution; results must be labelled "based on the STREAM benchmark" unless run per the STREAM Run Rules |

## Distribution Guidance

- For normal source distribution on GitHub, keep these tools as external
  dependencies and do not commit their binaries.
- For private or public binary bundles, review each bundled component's license
  and include the required notices and source materials.
- NVIDIA tools, CUDA libraries and drivers should be obtained from NVIDIA or
  the system package repositories, not redistributed inside this project.
- GPL-licensed command-line tools can generally be invoked as separate external
  programs, but bundling or modifying them creates additional obligations.

