# Copyright (C) 2005-2007 Jelmer Vernooij <jelmer@samba.org>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""Checkouts and working trees (working copies)."""

import bzrlib
from bzrlib import urlutils
from bzrlib.branch import PullResult
from bzrlib.bzrdir import BzrDirFormat, BzrDir
from bzrlib.errors import (InvalidRevisionId, NotBranchError, NoSuchFile,
                           NoRepositoryPresent, BzrError, UninitializableFormat,
                           OutOfDateTree)
from bzrlib.inventory import Inventory, InventoryFile, InventoryLink
from bzrlib.lockable_files import TransportLock, LockableFiles
from bzrlib.lockdir import LockDir
from bzrlib.osutils import file_kind, fingerprint_file, supports_executable
from bzrlib.revision import NULL_REVISION
from bzrlib.trace import mutter
from bzrlib.tree import RevisionTree
from bzrlib.transport.local import LocalTransport
from bzrlib.workingtree import WorkingTree, WorkingTreeFormat

from branch import SvnBranch
from commit import _revision_id_to_svk_feature
import constants
from convert import SvnConverter
from errors import LocalCommitsUnsupported, NoSvnRepositoryPresent
from mapping import (SVN_PROP_BZR_ANCESTRY, SVN_PROP_BZR_FILEIDS, 
                     SVN_PROP_BZR_REVISION_ID, SVN_PROP_BZR_REVISION_INFO,
                     generate_revision_metadata)
from remote import SvnRemoteAccess
from repository import SvnRepository
from svk import SVN_PROP_SVK_MERGE, parse_svk_features, serialize_svk_features
from mapping import escape_svn_path
from scheme import BranchingScheme
from transport import (SvnRaTransport, bzr_to_svn_url, svn_config) 
from tree import SvnBasisTree

import os
import urllib

import core, wc
from core import SubversionException, time_to_cstring

from format import get_rich_root_format


class SvnWorkingTree(WorkingTree):
    """WorkingTree implementation that uses a Subversion Working Copy for storage."""
    def __init__(self, bzrdir, local_path, branch):
        self._format = SvnWorkingTreeFormat()
        self.basedir = local_path
        self.bzrdir = bzrdir
        self._branch = branch
        self._get_wc()
        (min_rev, max_rev, switched, modified) = \
                wc.revision_status(self.basedir, None, True)

        self.base_tree = None
        self.base_revnum = max_rev
        if max_rev < 0:
            self.base_revid = None
            self._set_inventory(Inventory(), dirty=False)
        else:
            self.base_revid = branch.generate_revision_id(self.base_revnum)
            self.read_working_inventory()

        self.controldir = os.path.join(self.basedir, wc.get_adm_dir(), 
                                       'bzr')
        try:
            os.makedirs(self.controldir)
            os.makedirs(os.path.join(self.controldir, 'lock'))
        except OSError:
            pass
        control_transport = bzrdir.transport.clone(urlutils.join(
                                                   wc.get_adm_dir(), 'bzr'))
        self._control_files = LockableFiles(control_transport, 'lock', LockDir)

    def get_ignore_list(self):
        ignores = set([wc.get_adm_dir()])
        ignores.update(wc.get_default_ignores(svn_config))

        def dir_add(adm, prefix, patprefix):
            ignorestr = adm.prop_get(constants.PROP_IGNORE, 
                                    self.abspath(prefix).rstrip("/"))
            if ignorestr is not None:
                for pat in ignorestr.splitlines():
                    ignores.add(urlutils.joinpath(patprefix, pat))

            entries = adm.entries_read(False)
            for entry in entries:
                if entry == "":
                    continue

                if entries[entry].kind != core.NODE_DIR:
                    continue

                subprefix = os.path.join(prefix, entry)

                subwc = wc.WorkingCopy(adm, self.abspath(subprefix), False, 
                                         0, None)
                try:
                    dir_add(subwc, subprefix, urlutils.joinpath(patprefix, entry))
                finally:
                    subwc.close()

        adm = self._get_wc()
        try:
            dir_add(adm, "", ".")
        finally:
            adm.close()

        return ignores

    def is_control_filename(self, path):
        return wc.is_adm_dir(path)

    def apply_inventory_delta(self, changes):
        raise NotImplementedError(self.apply_inventory_delta)

    def _update(self, revnum=None):
        if revnum is None:
            revnum = self.branch.get_revnum()
        adm = self._get_wc()
        # FIXME: honor SVN_CONFIG_SECTION_HELPERS:SVN_CONFIG_OPTION_DIFF3_CMD
        # FIXME: honor SVN_CONFIG_SECTION_MISCELLANY:SVN_CONFIG_OPTION_USE_COMMIT_TIMES
        # FIXME: honor SVN_CONFIG_SECTION_MISCELLANY:SVN_CONFIG_OPTION_PRESERVED_CF_EXTS
        try:
            editor = adm.get_update_editor(self.basedir, use_commit_times=False, recurse=True)
            assert editor is not None
            reporter = self.branch.repository.transport.do_update(revnum, True, editor)
            adm.crawl_revisions(self.basedir, reporter, restore_files=False, recurse=True, 
                                use_commit_times=False)
            # FIXME: handle externals
        finally:
            adm.close()
        return revnum

    def update(self, change_reporter=None, possible_transports=None, revnum=None):
        orig_revnum = self.base_revnum
        self.base_revnum = self._update(revnum)
        self.base_revid = self.branch.generate_revision_id(self.base_revnum)
        self.base_tree = None
        self.read_working_inventory()
        return self.base_revnum - orig_revnum

    def remove(self, files, verbose=False, to_file=None):
        # FIXME: Use to_file argument
        # FIXME: Use verbose argument
        assert isinstance(files, list)
        adm = self._get_wc(write_lock=True)
        try:
            for file in files:
                adm.delete(self.abspath(file))
        finally:
            adm.close()

        for file in files:
            self._change_fileid_mapping(None, file)
        self.read_working_inventory()

    def _get_wc(self, relpath="", write_lock=False):
        return wc.WorkingCopy(None, self.abspath(relpath).rstrip("/"), 
                                write_lock, 0, None)

    def _get_rel_wc(self, relpath, write_lock=False):
        dir = os.path.dirname(relpath)
        file = os.path.basename(relpath)
        return (self._get_wc(dir, write_lock), file)

    def move(self, from_paths, to_dir=None, after=False, **kwargs):
        # FIXME: Use after argument
        assert after != True
        for entry in from_paths:
            try:
                to_wc = self._get_wc(to_dir, write_lock=True)
                to_wc.copy(self.abspath(entry), 
                            os.path.basename(entry), None, None)
            finally:
                to_wc.close()
            try:
                from_wc = self._get_wc(write_lock=True)
                from_wc.delete(self.abspath(entry))
            finally:
                from_wc.close()
            new_name = urlutils.join(to_dir, os.path.basename(entry))
            self._change_fileid_mapping(self.inventory.path2id(entry), new_name)
            self._change_fileid_mapping(None, entry)

        self.read_working_inventory()

    def rename_one(self, from_rel, to_rel, after=False):
        # FIXME: Use after
        assert after != True
        (to_wc, to_file) = self._get_rel_wc(to_rel, write_lock=True)
        if os.path.dirname(from_rel) == os.path.dirname(to_rel):
            # Prevent lock contention
            from_wc = to_wc
        else:
            (from_wc, _) = self._get_rel_wc(from_rel, write_lock=True)
        from_id = self.inventory.path2id(from_rel)
        try:
            to_wc.copy(self.abspath(from_rel), to_file)
            from_wc.delete(self.abspath(from_rel))
        finally:
            to_wc.close()
        self._change_fileid_mapping(None, from_rel)
        self._change_fileid_mapping(from_id, to_rel)
        self.read_working_inventory()

    def path_to_file_id(self, revnum, current_revnum, path):
        """Generate a bzr file id from a Subversion file name. 
        
        :param revnum: Revision number.
        :param path: Absolute path within the Subversion repository.
        :return: Tuple with file id and revision id.
        """
        assert isinstance(revnum, int) and revnum >= 0
        assert isinstance(path, str)

        rp = self.branch.unprefix(path)
        entry = self.basis_tree().id_map[rp]
        assert entry[0] is not None
        assert isinstance(entry[0], str), "fileid %r for %r is not a string" % (entry[0], path)
        return entry

    def read_working_inventory(self):
        inv = Inventory()

        def add_file_to_inv(relpath, id, revid, parent_id):
            """Add a file to the inventory."""
            assert isinstance(relpath, unicode)
            if os.path.islink(self.abspath(relpath)):
                file = InventoryLink(id, os.path.basename(relpath), parent_id)
                file.revision = revid
                file.symlink_target = os.readlink(self.abspath(relpath))
                file.text_sha1 = None
                file.text_size = None
                file.executable = False
                inv.add(file)
            else:
                file = InventoryFile(id, os.path.basename(relpath), parent_id)
                file.revision = revid
                try:
                    data = fingerprint_file(open(self.abspath(relpath)))
                    file.text_sha1 = data['sha1']
                    file.text_size = data['size']
                    file.executable = self.is_executable(id, relpath)
                    inv.add(file)
                except IOError:
                    # Ignore non-existing files
                    pass

        def find_copies(url, relpath=""):
            adm = self._get_wc(relpath)
            entries = adm.entries_read(False)
            for entry in entries.values():
                subrelpath = os.path.join(relpath, entry.name)
                if entry.name == "" or entry.kind != 'directory':
                    if ((entry.copyfrom_url == url or entry.url == url) and 
                        not (entry.schedule in (wc.SCHEDULE_DELETE,
                                                wc.SCHEDULE_REPLACE))):
                        yield os.path.join(
                                self.branch.get_branch_path().strip("/"), 
                                subrelpath)
                else:
                    find_copies(subrelpath)
            adm.close()

        def find_ids(entry, rootwc):
            relpath = urllib.unquote(entry.url[len(entry.repos):].strip("/"))
            assert entry.schedule in (wc.SCHEDULE_NORMAL, 
                                      wc.SCHEDULE_DELETE,
                                      wc.SCHEDULE_ADD,
                                      wc.SCHEDULE_REPLACE)
            if entry.schedule == wc.SCHEDULE_NORMAL:
                assert entry.revision >= 0
                # Keep old id
                return self.path_to_file_id(entry.cmt_rev, entry.revision, 
                        relpath)
            elif entry.schedule == wc.SCHEDULE_DELETE:
                return (None, None)
            elif (entry.schedule == wc.SCHEDULE_ADD or 
                  entry.schedule == wc.SCHEDULE_REPLACE):
                # See if the file this file was copied from disappeared
                # and has no other copies -> in that case, take id of other file
                if (entry.copyfrom_url and 
                    list(find_copies(entry.copyfrom_url)) == [relpath]):
                    return self.path_to_file_id(entry.copyfrom_rev, 
                        entry.revision, entry.copyfrom_url[len(entry.repos):])
                ids = self._get_new_file_ids(rootwc)
                if ids.has_key(relpath):
                    return (ids[relpath], None)
                # FIXME: Generate more random file ids
                return ("NEW-" + escape_svn_path(entry.url[len(entry.repos):].strip("/")), None)

        def add_dir_to_inv(relpath, adm, parent_id):
            assert isinstance(relpath, unicode)
            entries = adm.entries_read(False)
            entry = entries[""]
            assert parent_id is None or isinstance(parent_id, str), \
                    "%r is not a string" % parent_id
            (id, revid) = find_ids(entry, rootwc)
            if id is None:
                mutter('no id for %r' % entry.url)
                return
            assert revid is None or isinstance(revid, str), "%r is not a string" % revid
            assert isinstance(id, str), "%r is not a string" % id

            # First handle directory itself
            inv.add_path(relpath.decode("utf-8"), 'directory', id, parent_id).revision = revid
            if relpath == "":
                inv.revision_id = revid

            for name in entries:
                if name == "":
                    continue

                subrelpath = os.path.join(relpath, name.decode("utf-8"))

                entry = entries[name]
                assert entry
                
                if entry.kind == core.NODE_DIR:
                    subwc = wc.WorkingCopy(adm, self.abspath(subrelpath), 
                                             False, 0, None)
                    try:
                        add_dir_to_inv(subrelpath, subwc, id)
                    finally:
                        subwc.close()
                else:
                    (subid, subrevid) = find_ids(entry, rootwc)
                    if subid:
                        add_file_to_inv(subrelpath, subid, subrevid, id)
                    else:
                        mutter('no id for %r' % entry.url)

        rootwc = self._get_wc() 
        try:
            add_dir_to_inv(u"", rootwc, None)
        finally:
            rootwc.close()

        self._set_inventory(inv, dirty=False)
        return inv

    def set_last_revision(self, revid):
        mutter('setting last revision to %r' % revid)
        if revid is None or revid == NULL_REVISION:
            self.base_revid = revid
            self.base_revnum = 0
            self.base_tree = None
            return

        rev = self.branch.lookup_revision_id(revid)
        self.base_revnum = rev
        self.base_revid = revid
        self.base_tree = None

        # TODO: Implement more efficient version
        newrev = self.branch.repository.get_revision(revid)
        newrevtree = self.branch.repository.revision_tree(revid)

        def update_settings(adm, path):
            id = newrevtree.inventory.path2id(path)
            mutter("Updating settings for %r" % id)
            revnum = self.branch.lookup_revision_id(
                    newrevtree.inventory[id].revision)

            adm.process_committed(self.abspath(path).rstrip("/"), 
                          False, revnum, 
                          svn_time_to_cstring(newrev.timestamp), 
                          newrev.committer, None, False)

            if newrevtree.inventory[id].kind != 'directory':
                return

            entries = adm.entries_read(True)
            for entry in entries:
                if entry == "":
                    continue

                subwc = wc.WorkingCopy(adm, os.path.join(self.basedir, path, entry), False, 0, None)
                try:
                    update_settings(subwc, os.path.join(path, entry))
                finally:
                    subwc.close()

        # Set proper version for all files in the wc
        adm = self._get_wc(write_lock=True)
        try:
            update_settings(adm, "")
        finally:
            adm.close()
        self.base_revid = revid

    def commit(self, message=None, message_callback=None, revprops=None, 
               timestamp=None, timezone=None, committer=None, rev_id=None, 
               allow_pointless=True, strict=False, verbose=False, local=False, 
               reporter=None, config=None, specific_files=None, author=None):
        if author is not None:
            revprops['author'] = author
        # FIXME: Use allow_pointless
        # FIXME: Use verbose
        # FIXME: Use reporter
        # FIXME: Use strict
        if local:
            raise LocalCommitsUnsupported()

        if specific_files:
            specific_files = [self.abspath(x).encode('utf8') for x in specific_files]
        else:
            specific_files = [self.basedir.encode('utf8')]

        if message_callback is not None:
            def log_message_func(items):
                """ Simple log message provider for unit tests. """
                return message_callback(self).encode("utf-8")
        else:
            assert isinstance(message, basestring)
            def log_message_func(items):
                """ Simple log message provider for unit tests. """
                return message.encode("utf-8")

        import client
        self.client_ctx = client.Client()
        self.client_ctx.set_log_msg_func(log_message_func)
        if rev_id is not None:
            extra = "%d %s\n" % (self.branch.revno()+1, rev_id)
        else:
            extra = ""
        adm = self._get_wc(write_lock=True)
        try:
            adm.prop_set(SVN_PROP_BZR_REVISION_ID+str(self.branch.mapping.scheme), 
                             self._get_bzr_revids() + extra,
                             self.basedir)
            adm.prop_set(SVN_PROP_BZR_REVISION_INFO, 
                             generate_revision_metadata(timestamp, 
                                                        timezone, 
                                                        committer,
                                                        revprops),
                             self.basedir)
        finally:
            adm.close()

        try:
            try:
                commit_info = self.client_ctx.commit(specific_files, True, False)
            except SubversionException, (_, num):
                if num == constants.ERR_FS_TXN_OUT_OF_DATE:
                    raise OutOfDateTree(self)
                raise
        except:
            # Reset properties so the next subversion commit won't 
            # accidently set these properties.
            adm = self._get_wc(write_lock=True)
            adm.prop_set(SVN_PROP_BZR_REVISION_ID+str(self.branch.mapping.scheme), 
                             self._get_bzr_revids(), self.basedir)
            adm.prop_set(SVN_PROP_BZR_REVISION_INFO, 
                self.branch.repository.branchprop_list.get_property(
                self.branch.get_branch_path(self.base_revnum), 
                self.base_revnum, 
                SVN_PROP_BZR_REVISION_INFO, ""), 
                self.basedir)
            adm.close()
            raise

        self.client_ctx.set_log_msg_func(None)

        revid = self.branch.generate_revision_id(commit_info[0])

        self.base_revid = revid
        self.base_revnum = commit_info[0]
        self.base_tree = None

        return revid

    def smart_add(self, file_list, recurse=True, action=None, save=True):
        assert isinstance(recurse, bool)
        if action is None:
            action = bzrlib.add.AddAction()
        # TODO: use action
        if not file_list:
            # no paths supplied: add the entire tree.
            file_list = [u'.']
        ignored = {}
        added = []

        for file_path in file_list:
            todo = []
            file_path = os.path.abspath(file_path)
            f = self.relpath(file_path)
            adm = self._get_wc(os.path.dirname(f), write_lock=True)
            try:
                if not self.inventory.has_filename(f):
                    if save:
                        mutter('adding %r' % file_path)
                        adm.add(file_path, None, 0, None, None, None)
                    added.append(file_path)
                if recurse and file_kind(file_path) == 'directory':
                    # Filter out ignored files and update ignored
                    for c in os.listdir(file_path):
                        if self.is_control_filename(c):
                            continue
                        c_path = os.path.join(file_path, c)
                        ignore_glob = self.is_ignored(c)
                        if ignore_glob is not None:
                            ignored.setdefault(ignore_glob, []).append(c_path)
                        todo.append(c_path)
            finally:
                adm.close()
            if todo != []:
                cadded, cignored = self.smart_add(todo, recurse, action, save)
                added.extend(cadded)
                ignored.update(cignored)
        return added, ignored

    def add(self, files, ids=None, kinds=None):
        # TODO: Use kinds
        if isinstance(files, str):
            files = [files]
            if isinstance(ids, str):
                ids = [ids]
        if ids is not None:
            ids = iter(ids)
        assert isinstance(files, list)
        for f in files:
            adm = self._get_wc(os.path.dirname(f), write_lock=True)
            try:
                try:
                    adm.add(os.path.join(self.basedir, f))
                    if ids is not None:
                        self._change_fileid_mapping(ids.next(), f, adm)
                except SubversionException, (_, num):
                    if num == constants.ERR_ENTRY_EXISTS:
                        continue
                    elif num == constants.ERR_WC_PATH_NOT_FOUND:
                        raise NoSuchFile(path=f)
                    raise
            finally:
                adm.close()
        self.read_working_inventory()

    def basis_tree(self):
        if self.base_revid is None or self.base_revid == NULL_REVISION:
            return self.branch.repository.revision_tree(self.base_revid)

        if self.base_tree is None:
            self.base_tree = SvnBasisTree(self)

        return self.base_tree

    def pull(self, source, overwrite=False, stop_revision=None, 
             delta_reporter=None, possible_transports=None):
        # FIXME: Use delta_reporter
        # FIXME: Use source
        # FIXME: Use overwrite
        result = PullResult()
        result.source_branch = source
        result.master_branch = None
        result.target_branch = self.branch
        (result.old_revno, result.old_revid) = self.branch.last_revision_info()
        if stop_revision is None:
            stop_revision = self.branch.last_revision()
        revnumber = self.branch.lookup_revision_id(stop_revision)
        fetched = self._update(revnum)
        self.base_revnum = fetched
        self.base_revid = self.branch.generate_revision_id(fetched)
        self.base_tree = None
        self.read_working_inventory()
        result.new_revid = self.base_revid
        result.new_revno = self.branch.revision_id_to_revno(result.new_revid)
        return result

    def get_file_sha1(self, file_id, path=None, stat_value=None):
        if not path:
            path = self._inventory.id2path(file_id)
        return fingerprint_file(open(self.abspath(path)))['sha1']

    def _change_fileid_mapping(self, id, path, adm=None):
        if adm is None:
            subwc = self._get_wc(write_lock=True)
        else:
            subwc = adm 
        new_entries = self._get_new_file_ids(subwc)
        if id is None:
            if new_entries.has_key(path):
                del new_entries[path]
        else:
            assert isinstance(id, str)
            new_entries[path] = id
        existing = "".join(map(lambda (path, id): "%s\t%s\n" % (path, id), new_entries.items()))
        if existing != "":
            subwc.prop_set(SVN_PROP_BZR_FILEIDS, existing.encode("utf-8"), self.basedir)
        if adm is None:
            subwc.close()

    def _get_new_file_ids(self, adm):
        committed = self.branch.repository.branchprop_list.get_property(
                self.branch.get_branch_path(self.base_revnum), self.base_revnum, 
                SVN_PROP_BZR_FILEIDS, "")
        existing = adm.prop_get(SVN_PROP_BZR_FILEIDS, self.basedir)
        if existing is None or committed == existing:
            return {}
        return dict(map(lambda x: str(x).split("\t"), 
            existing.splitlines()))

    def _get_bzr_revids(self):
        return self.branch.repository.branchprop_list.get_property(
                self.branch.get_branch_path(self.base_revnum), 
                self.base_revnum, 
                SVN_PROP_BZR_REVISION_ID+str(self.branch.mapping.scheme), "")

    def _get_bzr_merges(self):
        return self.branch.repository.branchprop_list.get_property(
                self.branch.get_branch_path(self.base_revnum), 
                self.base_revnum, SVN_PROP_BZR_ANCESTRY+str(self.branch.mapping.scheme), "")

    def _get_svk_merges(self):
        return self.branch.repository.branchprop_list.get_property(
                self.branch.get_branch_path(self.base_revnum), 
                self.base_revnum, SVN_PROP_SVK_MERGE, "")

    def set_pending_merges(self, merges):
        """See MutableTree.set_pending_merges()."""
        adm = self._get_wc(write_lock=True)
        try:
            # Set bzr:merge
            if len(merges) > 0:
                bzr_merge = "\t".join(merges) + "\n"
            else:
                bzr_merge = ""

            adm.prop_set(SVN_PROP_BZR_ANCESTRY+str(self.branch.mapping.scheme), 
                                 self._get_bzr_merges() + bzr_merge, 
                                 self.basedir)
            
            svk_merges = parse_svk_features(self._get_svk_merges())

            # Set svk:merge
            for merge in merges:
                try:
                    svk_merges.add(_revision_id_to_svk_feature(merge))
                except InvalidRevisionId:
                    pass

            adm.prop_set(SVN_PROP_SVK_MERGE, 
                             serialize_svk_features(svk_merges), self.basedir, 
                             False)
        finally:
            adm.close()

    def add_pending_merge(self, revid):
        merges = self.pending_merges()
        merges.append(revid)
        self.set_pending_merges(merges)

    def pending_merges(self):
        merged = self._get_bzr_merges().splitlines()
        adm = self._get_wc()
        try:
            merged_data = adm.prop_get(
                SVN_PROP_BZR_ANCESTRY+str(self.branch.mapping.scheme), self.basedir)
            if merged_data is None:
                set_merged = []
            else:
                set_merged = merged_data.splitlines()
        finally:
            adm.close()

        assert (len(merged) == len(set_merged) or 
               len(merged)+1 == len(set_merged))

        if len(set_merged) > len(merged):
            return set_merged[-1].split("\t")

        return []

    def _reset_data(self):
        pass

    def unlock(self):
        # non-implementation specific cleanup
        self._cleanup()

        # reverse order of locking.
        try:
            return self._control_files.unlock()
        finally:
            self.branch.unlock()

    if not supports_executable():
        def is_executable(self, file_id, path=None):
            inv = self.basis_tree()._inventory
            if file_id in inv:
                return inv[file_id].executable
            # Default to not executable
            return False


class SvnWorkingTreeFormat(WorkingTreeFormat):
    """Subversion working copy format."""
    def __get_matchingbzrdir(self):
        return SvnWorkingTreeDirFormat()

    _matchingbzrdir = property(__get_matchingbzrdir)

    def get_format_description(self):
        return "Subversion Working Copy"

    def get_format_string(self):
        return "Subversion Working Copy Format"

    def initialize(self, a_bzrdir, revision_id=None):
        raise NotImplementedError(self.initialize)

    def open(self, a_bzrdir):
        raise NotImplementedError(self.initialize)


class SvnCheckout(BzrDir):
    """BzrDir implementation for Subversion checkouts (directories 
    containing a .svn subdirectory."""
    def __init__(self, transport, format):
        super(SvnCheckout, self).__init__(transport, format)
        self.local_path = transport.local_abspath(".")
        
        # Open related remote repository + branch
        adm = wc.WorkingCopy(None, self.local_path, False, 0, None)
        try:
            svn_url = adm.entry(self.local_path, True).url
        finally:
            adm.close()

        remote_transport = SvnRaTransport(svn_url)
        self.remote_bzrdir = SvnRemoteAccess(remote_transport)
        self.svn_root_transport = remote_transport.clone_root()
        self.root_transport = self.transport = transport
        
    def clone(self, path, revision_id=None, force_new_repo=False):
        raise NotImplementedError(self.clone)

    def open_workingtree(self, _unsupported=False, recommend_upgrade=False):
        return SvnWorkingTree(self, self.local_path, self.open_branch())

    def sprout(self, url, revision_id=None, force_new_repo=False, 
               recurse='down', possible_transports=None, accelerator_tree=None,
               hardlink=False):
        # FIXME: honor force_new_repo
        # FIXME: Use recurse
        result = get_rich_root_format().initialize(url)
        repo = self._find_repository()
        repo.clone(result, revision_id)
        branch = self.open_branch()
        branch.sprout(result, revision_id)
        result.create_workingtree(hardlink=hardlink)
        return result

    def open_repository(self):
        raise NoRepositoryPresent(self)

    def find_repository(self):
        raise NoRepositoryPresent(self)

    def _find_repository(self):
        return SvnRepository(self, self.svn_root_transport, 
                             self.remote_bzrdir.branch_path)

    def needs_format_conversion(self, format=None):
        if format is None:
            format = BzrDirFormat.get_default_format()
        return not isinstance(self._format, format.__class__)

    def create_workingtree(self, revision_id=None, hardlink=None):
        """See BzrDir.create_workingtree().

        Not implemented for Subversion because having a .svn directory
        implies having a working copy.
        """
        raise NotImplementedError(self.create_workingtree)

    def create_branch(self):
        """See BzrDir.create_branch()."""
        raise NotImplementedError(self.create_branch)

    def open_branch(self, unsupported=True):
        """See BzrDir.open_branch()."""
        repos = self._find_repository()

        try:
            branch = SvnBranch(self.svn_root_transport.base, repos, 
                               self.remote_bzrdir.branch_path)
        except SubversionException, (_, num):
            if num == constants.ERR_WC_NOT_DIRECTORY:
                raise NotBranchError(path=self.base)
            raise

        branch.bzrdir = self.remote_bzrdir
 
        return branch



