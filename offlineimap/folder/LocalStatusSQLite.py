# Local status cache virtual folder: SQLite backend
# Copyright (C) 2009-2017 Stewart Smith and contributors.
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA

import os
import sqlite3 as sqlite
from sys import exc_info
from threading import Lock

import six

from .Base import BaseFolder


class DatabaseFileLock(object):
    """Lock at database file level."""

    def __init__(self):
        self._lock = Lock()
        self._counter = 0

    def __enter__(self):
        self._lock.acquire()

    def __exit__(self, typ, value, tb):
        self._lock.release()

    def registerNewUser(self):
        self._counter += 1

    def removeOneUser(self):
        self._counter -= 1

    def getLock(self):
        return self._lock

    def shouldClose(self):
        return self._counter < 1


class LocalStatusSQLiteFolder(BaseFolder):
    """LocalStatus backend implemented with an SQLite database

    As python-sqlite currently does not allow to access the same sqlite
    objects from various threads, we need to open get and close a db
    connection and cursor for all operations. This is a big disadvantage
    and we might want to investigate if we cannot hold an object open
    for a thread somehow."""
    # Though. According to sqlite docs, you need to commit() before
    # the connection is closed or your changes will be lost!
    # get db connection which autocommits
    # connection = sqlite.connect(self.filename, isolation_level=None)
    # cursor = connection.cursor()
    # return connection, cursor

    # Current version of our db format.
    cur_version = 2
    # Keep track on how many threads need access to the database.
    locks = {} # Key: filename, value: DatabaseFileLock instance.

    def __init__(self, name, repository):
        self.sep = '.' # Needs to be set before super().__init__().
        super(LocalStatusSQLiteFolder, self).__init__(name, repository)
        self.root = repository.root
        self.filename = os.path.join(self.getroot(), self.getfolderbasename())

        self._newfolder = False        # Flag if the folder is new.

        dirname = os.path.dirname(self.filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        if not os.path.isdir(dirname):
            raise UserWarning("SQLite database path '%s' is not a directory."%
                dirname)

        self.connection = None
        # The lock serialize the writing/open/close of database accross threads.
        if self.filename not in LocalStatusSQLiteFolder.locks:
            LocalStatusSQLiteFolder.locks[self.filename] = DatabaseFileLock()
        self._databaseFileLock = LocalStatusSQLiteFolder.locks[self.filename]
        self._in_transactions = 0

    def __enter__(self):
        if not self.dofsync():
            assert self.connection is not None
            self._in_transactions += 1

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.dofsync():
            assert self._in_transactions > 0
            self._in_transactions -= 1
            if self._in_transactions < 1:
                self.connection.commit()

    def openfiles(self):
        # Make sure sqlite is in multithreading SERIALIZE mode.
        assert sqlite.threadsafety == 1, 'Your sqlite is not multithreading safe.'

        with self._databaseFileLock.getLock():
            # Try to establish connection, no need for threadsafety in __init__.
            try:
                self.connection = sqlite.connect(self.filename,
                                                 check_same_thread=False)
                self._databaseFileLock.registerNewUser()
            except sqlite.OperationalError as e:
                # Operation had failed.
                six.reraise(UserWarning,
                            UserWarning(
                                "cannot open database file '%s': %s.\nYou might"
                                " want to check the rights to that file and if "
                                "it cleanly opens with the 'sqlite<3>' command"%
                                (self.filename, e)),
                            exc_info()[2])

            # Test if db version is current enough and if db is readable.
            try:
                cursor = self.connection.execute(
                    "SELECT value from metadata WHERE key='db_version'")
            except sqlite.DatabaseError:
                # db file missing or corrupt, recreate it.
                self.__create_db()
            else:
                # Fetch db version and upgrade if needed.
                version = int(cursor.fetchone()[0])
                if version < LocalStatusSQLiteFolder.cur_version:
                    self.__upgrade_db(version)

    def purge(self):
        """Remove any pre-existing database. Do not call in dry-run mode."""

        try:
            os.unlink(self.filename)
        except OSError as e:
            self.ui.debug('', "could not remove file %s: %s"%
                (self.filename, e))

    def storesmessages(self):
        return False

    def getfullname(self):
        return self.filename

    # Interface from LocalStatusFolder
    def isnewfolder(self):
        return self._newfolder

    def __sql_write(self, sql, args=None, executemany=False):
        """Execute some SQL, retrying if the db was locked.

        :param sql: the SQL string passed to execute()
        :param args: the variable values to `sql`. E.g. (1,2) or {uid:1,
            flags:'T'}. See sqlite docs for possibilities.
        :param executemany: bool indicating whether we want to
            perform conn.executemany() or conn.execute().
        :returns: None or raises an Exception."""

        success = False
        while not success:
            try:
                with self._databaseFileLock.getLock():
                    if args is None:
                        if executemany:
                            self.connection.executemany(sql)
                        else:
                            self.connection.execute(sql)
                    else:
                        if executemany:
                            self.connection.executemany(sql, args)
                        else:
                            self.connection.execute(sql, args)
                    success = True
                    if not self._in_transactions:
                        self.connection.commit()
            except sqlite.OperationalError as e:
                if e.args[0] == 'cannot commit - no transaction is active':
                    pass
                elif e.args[0] == 'database is locked':
                    self.ui.debug('', "Locked sqlite database, retrying.")
                    success = False
                else:
                    raise

    def __upgrade_db(self, from_ver):
        """Upgrade the sqlite format from version 'from_ver' to current"""

        if self.connection is not None:
            self.connection.close() # Close old connections first.
        self.connection = sqlite.connect(self.filename,
                                         check_same_thread=False)

        # Upgrade from database version 1 to version 2
        # This change adds labels and mtime columns, to be used by Gmail IMAP and Maildir folders.
        if from_ver <= 1:
            self.ui._msg('Upgrading LocalStatus cache from version 1 to version 2 for %s:%s'%
                (self.repository, self))
            self.connection.executescript("""ALTER TABLE status ADD mtime INTEGER DEFAULT 0;
                                             ALTER TABLE status ADD labels VARCHAR(256) DEFAULT '';
                                             UPDATE metadata SET value='2' WHERE key='db_version';
                                          """)
            self.connection.commit()

        # Future version upgrades come here...
        # if from_ver <= 2: ... #upgrade from 2 to 3
        # if from_ver <= 3: ... #upgrade from 3 to 4


    def __create_db(self):
        """Create a new db file.

        self.connection must point to the opened and valid SQlite
        database connection."""
        self.ui._msg('Creating new Local Status db for %s:%s'%
                     (self.repository, self))
        self.connection.executescript("""
        CREATE TABLE metadata (key VARCHAR(50) PRIMARY KEY, value VARCHAR(128));
        INSERT INTO metadata VALUES('db_version', '2');
        CREATE TABLE status (id INTEGER PRIMARY KEY, flags VARCHAR(50), mtime INTEGER, labels VARCHAR(256));
        """)
        self.connection.commit()
        self._newfolder = True


    # Interface from BaseFolder
    def msglist_item_initializer(self, uid):
        return {'uid': uid, 'flags': set(), 'labels': set(), 'time': 0, 'mtime': 0}


    # Interface from BaseFolder
    def cachemessagelist(self):
        self.dropmessagelistcache()
        cursor = self.connection.execute('SELECT id,flags,mtime,labels from status')
        for row in cursor:
            uid = row[0]
            self.messagelist[uid] = self.msglist_item_initializer(uid)
            flags = set(row[1])
            try:
                labels = set([lb.strip() for lb in
                    row[3].split(',') if len(lb.strip()) > 0])
            except AttributeError:
                # FIXME: This except clause was introduced because row[3] from
                # database can be found of unexpected type NoneType. See
                # https://github.com/OfflineIMAP/offlineimap/issues/103
                #
                # We are fixing the type here but this would require more
                # researches to find the true root cause. row[3] is expected to
                # be a (empty) string, not None.
                #
                # Also, since database might return None, we have to fix the
                # database, too.
                labels = set()
            self.messagelist[uid]['flags'] = flags
            self.messagelist[uid]['labels'] = labels
            self.messagelist[uid]['mtime'] = row[2]

    def closefiles(self):
        with self._databaseFileLock.getLock():
            self._databaseFileLock.removeOneUser()
            if self._databaseFileLock.shouldClose():
                try:
                    self.connection.close()
                except:
                    pass

    # Interface from LocalStatusFolder
    def save(self):
        pass
        # Noop. every transaction commits to database!

    def saveall(self):
        """Saves the entire messagelist to the database."""

        with self._databaseFileLock.getLock():
            data = []
            for uid, msg in self.messagelist.items():
                mtime = msg['mtime']
                flags = ''.join(sorted(msg['flags']))
                labels = ', '.join(sorted(msg['labels']))
                data.append((uid, flags, mtime, labels))

            self.__sql_write('INSERT OR REPLACE INTO status '
                '(id,flags,mtime,labels) VALUES (?,?,?,?)',
                data, executemany=True)


    # Following some pure SQLite functions, where we chose to use
    # BaseFolder() methods instead. Doing those on the in-memory list is
    # quicker anyway. If our db becomes so big that we don't want to
    # maintain the in-memory list anymore, these might come in handy
    # in the future though.
    #
    #def uidexists(self,uid):
    #    conn, cursor = self.get_cursor()
    #    with conn:
    #        cursor.execute('SELECT id FROM status WHERE id=:id',{'id': uid})
    #        return cursor.fetchone()
    # This would be the pure SQLite solution, use BaseFolder() method,
    # to avoid threading with sqlite...
    #def getmessageuidlist(self):
    #    conn, cursor = self.get_cursor()
    #    with conn:
    #        cursor.execute('SELECT id from status')
    #        r = []
    #        for row in cursor:
    #            r.append(row[0])
    #        return r
    #def getmessagecount(self):
    #    conn, cursor = self.get_cursor()
    #    with conn:
    #        cursor.execute('SELECT count(id) from status');
    #        return cursor.fetchone()[0]
    #def getmessageflags(self, uid):
    #    conn, cursor = self.get_cursor()
    #    with conn:
    #        cursor.execute('SELECT flags FROM status WHERE id=:id',
    #                        {'id': uid})
    #        for row in cursor:
    #            flags = [x for x in row[0]]
    #            return flags
    #        assert False,"getmessageflags() called on non-existing message"


    # Interface from BaseFolder
    def savemessage(self, uid, content, flags, rtime, mtime=0, labels=set()):
        """Writes a new message, with the specified uid.

        See folder/Base for detail. Note that savemessage() does not
        check against dryrun settings, so you need to ensure that
        savemessage is never called in a dryrun mode."""

        if uid < 0:
            # We cannot assign a uid.
            return uid

        if self.uidexists(uid):     # Already have it.
            self.savemessageflags(uid, flags)
            return uid

        self.messagelist[uid] = self.msglist_item_initializer(uid)
        self.messagelist[uid] = {'uid': uid, 'flags': flags, 'time': rtime, 'mtime': mtime, 'labels': labels}
        flags = ''.join(sorted(flags))
        labels = ', '.join(sorted(labels))
        try:
            self.__sql_write('INSERT INTO status (id,flags,mtime,labels) VALUES (?,?,?,?)',
                            (uid,flags,mtime,labels))
        except Exception as e:
            six.reraise(UserWarning,
                        UserWarning("%s while inserting UID %s"%
                            (str(e), str(uid))),
                        exc_info()[2])
        return uid


    # Interface from BaseFolder
    def savemessageflags(self, uid, flags):
        assert self.uidexists(uid)
        self.messagelist[uid]['flags'] = flags
        flags = ''.join(sorted(flags))
        self.__sql_write('UPDATE status SET flags=? WHERE id=?',(flags,uid))


    def getmessageflags(self, uid):
        return self.messagelist[uid]['flags']


    def savemessagelabels(self, uid, labels, mtime=None):
        self.messagelist[uid]['labels'] = labels
        if mtime: self.messagelist[uid]['mtime'] = mtime

        labels = ', '.join(sorted(labels))
        if mtime:
            self.__sql_write('UPDATE status SET labels=?, mtime=? WHERE id=?',(labels,mtime,uid))
        else:
            self.__sql_write('UPDATE status SET labels=? WHERE id=?',(labels,uid))


    def savemessageslabelsbulk(self, labels):
        """
        Saves labels from a dictionary in a single database operation.

        """
        data = [(', '.join(sorted(l)), uid) for uid, l in labels.items()]
        self.__sql_write('UPDATE status SET labels=? WHERE id=?', data, executemany=True)
        for uid, l in labels.items():
            self.messagelist[uid]['labels'] = l


    def addmessageslabels(self, uids, labels):
        data = []
        for uid in uids:
            newlabels = self.messagelist[uid]['labels'] | labels
            data.append((', '.join(sorted(newlabels)), uid))
        self.__sql_write('UPDATE status SET labels=? WHERE id=?', data, executemany=True)
        for uid in uids:
            self.messagelist[uid]['labels'] = self.messagelist[uid]['labels'] | labels


    def deletemessageslabels(self, uids, labels):
        data = []
        for uid in uids:
            newlabels = self.messagelist[uid]['labels'] - labels
            data.append((', '.join(sorted(newlabels)), uid))
        self.__sql_write('UPDATE status SET labels=? WHERE id=?', data, executemany=True)
        for uid in uids:
            self.messagelist[uid]['labels'] = self.messagelist[uid]['labels'] - labels


    def getmessagelabels(self, uid):
        return self.messagelist[uid]['labels']


    def savemessagesmtimebulk(self, mtimes):
        """Saves mtimes from the mtimes dictionary in a single database operation."""

        data = [(mt, uid) for uid, mt in mtimes.items()]
        self.__sql_write('UPDATE status SET mtime=? WHERE id=?', data, executemany=True)
        for uid, mt in mtimes.items():
            self.messagelist[uid]['mtime'] = mt


    def getmessagemtime(self, uid):
        return self.messagelist[uid]['mtime']


    # Interface from BaseFolder
    def deletemessage(self, uid):
        if not uid in self.messagelist:
            return
        self.__sql_write('DELETE FROM status WHERE id=?', (uid, ))
        del(self.messagelist[uid])

    # Interface from BaseFolder
    def deletemessages(self, uidlist):
        """Delete list of UIDs from status cache

        This function uses sqlites executemany() function which is
        much faster than iterating through deletemessage() when we have
        many messages to delete."""

        # Weed out ones not in self.messagelist
        uidlist = [uid for uid in uidlist if uid in self.messagelist]
        if not len(uidlist):
            return
        # arg2 needs to be an iterable of 1-tuples [(1,),(2,),...]
        self.__sql_write('DELETE FROM status WHERE id=?', list(zip(uidlist, )), True)
        for uid in uidlist:
            del(self.messagelist[uid])


    # Function to check if all entries can be read
    def check_read_entries(SELF):
        connection = sqlite3.connect(self.filename)
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM entries")
        entries = cursor.fetchall()
        cursor.close()
        connection.close()
        return len(entries) > 0

    # Function to insert a fake entry
    def insert_fake_entry(self):
        connection = sqlite3.connect(self.filename)
        cursor = connection.cursor()
        cursor.execute("INSERT INTO entries (title, content) VALUES ('Fake Entry', 'This is a fake entry.')")
        connection.commit()
        cursor.close()
        connection.close()

    # Function to remove the fake entry
    def remove_fake_entry(self):
        connection = sqlite3.connect(self.filename)
        cursor = connection.cursor()
        cursor.execute("DELETE FROM entries WHERE title='Fake Entry'")
        connection.commit()
        cursor.close()
        connection.close()

    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Database management tool")
    parser.add_argument("database", help="the path to the SQLite database")
    parser.add_argument("--check-database", action="store_true", help="check database integrity")
    args = parser.parse_args()



     
