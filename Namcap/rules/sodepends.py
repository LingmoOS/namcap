# Copyright (C) 2003-2023 Namcap contributors, see AUTHORS for details.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Checks dependencies resulting from linking of shared libraries."""

from collections import defaultdict
import re
import os
import subprocess
from typing import Literal, TypeAlias

import Namcap.package
from Namcap.ruleclass import TarballRule
from Namcap.util import is_elf
from Namcap.rules.rpath import get_rpaths
from Namcap.rules.runpath import get_runpaths

from elftools.elf.elffile import ELFFile
from elftools.elf.dynamic import DynamicSection

Architecture: TypeAlias = Literal["i686", "x86-64"]

libcache: dict[Architecture, dict[str, str]] = {"i686": {}, "x86-64": {}}

_DependsMap: TypeAlias = dict[str, str]
_LibMap: TypeAlias = dict[str, set[str]]
_ProvidesMap: TypeAlias = dict[str, set[str]]


def scanlibs(fileobj, filename, custom_libs, liblist, libdepends, libprovides):
    """
    Find shared libraries in a file-like binary object

    If it depends on a library or provides one, store that library's path.
    """

    if not is_elf(fileobj):
        return {}

    elffile = ELFFile(fileobj)
    for section in elffile.iter_sections():
        if not isinstance(section, DynamicSection):
            continue
        for tag in section.iter_tags():
            bitsize = elffile.elfclass
            match bitsize:
                case 32:
                    architecture: Architecture = "i686"
                case 64:
                    architecture = "x86-64"
            # DT_SONAME means it provides a library
            if tag.entry.d_tag == "DT_SONAME" and os.path.dirname(filename) in ["usr/lib", "usr/lib32"]:
                soname = re.sub(r"\.so.*", ".so", tag.soname)
                soversion = re.sub(r"^.*\.so\.", "", tag.soname)
                libprovides[soname + "=" + soversion + "-" + str(bitsize)].add(filename)
            # DT_NEEDED means shared library
            if tag.entry.d_tag != "DT_NEEDED":
                continue
            libname = tag.needed
            soname = re.sub(r"\.so.*", ".so", libname)
            soversion = re.sub(r"^.*\.so\.", "", libname)
            if libname in custom_libs:
                libpath = custom_libs[libname][1:]
                continue
            try:
                libpath = os.path.abspath(libcache[architecture][libname])[1:]
            except KeyError:
                # We didn't know about the library, so add it for fail later
                libpath = libname
            libdepends[soname + "=" + soversion + "-" + str(bitsize)] = libpath
            liblist[libpath].add(filename)


def finddepends(libdepends):
    """
    Find packages owning a list of libraries

    Returns:
      dependlist -- a dictionary { package => set(libraries) }
      libdependlist -- a dictionary { soname => package }
      orphans -- the list of libraries without owners
      missing_provides -- the list of sonames without providers
    """
    dependlist = defaultdict(set)
    libdependlist = {}
    missing_provides = {}

    actualpath = {}

    knownlibs = set(libdepends)
    foundlibs = set()

    actualpath = dict((j, os.path.realpath("/" + libdepends[j])[1:]) for j in knownlibs)

    # Sometimes packages don't include all so .so, .so.1, .so.1.13, .so.1.13.19 files
    # They rely on ldconfig to create all the symlinks
    # So we will strip off the matching part of the files and use this regexp to match the rest
    so_end = re.compile(r"(\.\d+)*")
    # Whether we should even look at a particular file
    is_so = re.compile(r"\.so")

    for pkg in Namcap.package.get_installed_packages():
        for j, fsize, fmode in pkg.files:
            if not is_so.search(j):
                continue

            for k in knownlibs:
                # File must be an exact match or have the right .so ending numbers
                # i.e. gpm includes libgpm.so and libgpm.so.1.19.0, but everything links to libgpm.so.1
                # We compare find libgpm.so.1.19.0 startswith libgpm.so.1 and .19.0 matches the regexp
                if j == actualpath[k] or (j.startswith(actualpath[k]) and so_end.match(j[len(actualpath[k]) :])):
                    dependlist[pkg.name].add(libdepends[k])
                    foundlibs.add(k)
                    # Check if the dependency can be satisfied by soname
                    if k in pkg.provides:
                        libdependlist[k] = pkg.name
                    else:
                        missing_provides[k] = pkg.name

    orphans = list(knownlibs - foundlibs)
    return dependlist, libdependlist, orphans, missing_provides


def filllibcache():
    var = subprocess.Popen(
        "ldconfig -p", env={"LANG": "C"}, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).communicate()
    libline = re.compile(r"\s*(.*) \((.*)\) => (.*)")
    for j in var[0].decode("ascii").splitlines():
        g = libline.match(j)
        if g is not None:
            if g.group(2).startswith("libc6,x86-64"):
                libcache["x86-64"][g.group(1)] = g.group(3)
            else:
                # TODO: This is bogus; what do non x86-architectures print?
                libcache["i686"][g.group(1)] = g.group(3)


class SharedLibsRule(TarballRule):
    name = "sodepends"
    description = "Checks dependencies caused by linked shared libraries"

    def analyze(self, pkginfo, tar):
        liblist: _LibMap = defaultdict(set)
        libdepends: _DependsMap = defaultdict(str)
        libprovides: _ProvidesMap = defaultdict(set)
        dependlist = {}
        libdependlist = {}
        missing_provides = {}
        filllibcache()
        os.environ["LC_ALL"] = "C"
        pkg_so_files = ["/" + n for n in tar.getnames() if ".so" in n]

        for entry in tar:
            if not entry.isfile():
                continue
            f = tar.extractfile(entry)
            # find anything that could be rpath related
            rpath_files = {}
            if is_elf(f):
                rpaths = list(get_rpaths(f)) + list(get_runpaths(f))
                f.seek(0)
                for n in pkg_so_files:
                    for rp in rpaths:
                        rp = os.path.normpath(rp.replace("$ORIGIN", "/" + os.path.dirname(entry.path)))
                        if os.path.dirname(n) == rp:
                            rpath_files[os.path.basename(n)] = n
            scanlibs(f, entry.name, rpath_files, liblist, libdepends, libprovides)
            f.close()

        # Ldd all the files and find all the link and script dependencies
        dependlist, libdependlist, orphans, missing_provides = finddepends(libdepends)

        # Filter out internal dependencies
        libdependlist = dict(filter(lambda elem: elem[1] != pkginfo["name"], libdependlist.items()))
        missing_provides = dict(filter(lambda elem: elem[1] != pkginfo["name"], missing_provides.items()))

        # Handle "no package associated" errors
        self.warnings.extend(
            [
                ("library-no-package-associated %s %s", (libdepends[i], str(list(liblist[libdepends[i]]))))
                for i in orphans
            ]
        )

        # Hanle when a required soname does not provided by the associated package yet
        self.infos.extend(
            [
                ("libdepends-missing-provides %s %s (%s)", (i, missing_provides[i], str(list(liblist[libdepends[i]]))))
                for i in missing_provides
            ]
        )

        # Print link-level deps
        for pkg, libraries in dependlist.items():
            if isinstance(libraries, set):
                files = list(libraries)
                needing = set().union(*[liblist[lib] for lib in libraries])
                reasons = pkginfo.detected_deps.setdefault(pkg, [])
                reasons.append(("libraries-needed %s %s", (str(files), str(list(needing)))))
                self.infos.append(("link-level-dependence %s in %s", (pkg, str(files))))

        # Check for soname dependencies
        for i in libdependlist:
            if i in pkginfo["depends"]:
                self.infos.append(
                    (
                        "libdepends-detected-satisfied %s %s (%s)",
                        (i, libdependlist[i], str(list(liblist[libdepends[i]]))),
                    )
                )
                continue
            if i in pkginfo["optdepends"]:
                self.infos.append(
                    (
                        "libdepends-detected-but-optional %s %s (%s)",
                        (i, libdependlist[i], str(list(liblist[libdepends[i]]))),
                    )
                )
                continue
            self.infos.append(
                (
                    "libdepends-detected-not-included %s %s (%s)",
                    (i, libdependlist[i], str(list(liblist[libdepends[i]]))),
                )
            )

        for i in pkginfo["depends"]:
            if ".so" in i and i not in libdependlist:
                self.warnings.append(("libdepends-not-needed %s", (i,)))
            if i.endswith(".so"):
                self.errors.append(("libdepends-without-version %s", (i,)))

        self.infos.append(
            (
                "libdepends-by-namcap-sight depends=(%s)",
                (" ".join(sorted(set(libdependlist) | set(missing_provides.values()))),),
            )
        )

        # Check provided libraries
        for i in libprovides:
            if i in pkginfo["provides"]:
                self.infos.append(("libprovides-satisfied %s %s", (i, str(list(libprovides[i])))))
                continue
            self.infos.append(("libprovides-unsatisfied %s %s", (i, str(list(libprovides[i])))))

        for i in pkginfo["provides"]:
            if ".so" in i and i not in libprovides:
                self.warnings.append(("libprovides-missing %s", (i,)))
            if i.endswith(".so"):
                self.errors.append(("libprovides-without-version %s", (i,)))

        self.infos.append(("libprovides-by-namcap-sight provides=(%s)", (" ".join(libprovides),)))

        # Check for packages in testing
        for i in dependlist.keys():
            p = Namcap.package.load_testing_package(i)
            q = Namcap.package.load_from_db(i)
            if p is not None and q is not None and p["version"] == q["version"]:
                self.warnings.append(("dependency-is-testing-release %s", (i,)))
