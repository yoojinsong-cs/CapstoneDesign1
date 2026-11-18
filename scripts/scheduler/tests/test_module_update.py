import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scheduler.installer import ensure_module


class ModuleUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.loaded, self.kernel = root / 'loaded', root / 'kernel'
        (self.loaded / 'parameters').mkdir(parents=True)
        (self.loaded / 'parameters/server_port').write_text('5001\n')
        (self.loaded / 'refcnt').write_text('0\n')
        self.kernel.mkdir()
        for filename in ('sch_flowprio.c', 'Makefile', 'sch_flowprio.ko'):
            (self.kernel / filename).write_text('test fixture\n')
        for target, value in (('MODULE_STATE', self.loaded), ('KERNEL_DIRECTORY', self.kernel)):
            patcher = patch('scheduler.installer.' + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('scheduler.installer.os.geteuid', return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def modinfo(self, argv, **kwargs):
        return os.uname().release if argv[2] == 'vermagic' else '3\n'

    def test_current_loaded_policy_is_reused(self):
        (self.loaded / 'version').write_text('3\n')
        with patch('scheduler.installer.subprocess.run') as command:
            ensure_module(5001)
        command.assert_not_called()

    def test_old_policy_in_use_and_different_port_are_left_untouched(self):
        with patch('scheduler.installer.subprocess.run') as command:
            (self.loaded / 'refcnt').write_text('1\n')
            with self.assertRaisesRegex(RuntimeError, 'in use'):
                ensure_module(5001)
            with self.assertRaisesRegex(RuntimeError, 'server port'):
                ensure_module(6000)
        command.assert_not_called()

    def test_unused_old_policy_is_replaced_without_forcing_removal(self):
        with patch('scheduler.installer.subprocess.run') as command, \
             patch('scheduler.installer.subprocess.check_output', side_effect=self.modinfo), \
             patch('sys.stdout', new_callable=io.StringIO):
            ensure_module(5001)
        self.assertEqual([c.args[0] for c in command.call_args_list],
                         [['rmmod', 'sch_flowprio'],
                          ['insmod', str(self.kernel / 'sch_flowprio.ko'), 'server_port=5001']])

    def test_build_failure_leaves_old_loaded_module_in_place(self):
        (self.kernel / 'sch_flowprio.ko').unlink()
        with patch('scheduler.installer.shutil.which', return_value='/usr/bin/gcc-12'), \
             patch('scheduler.installer.subprocess.run',
                   side_effect=subprocess.CalledProcessError(1, 'make')) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                ensure_module(5001)
        self.assertEqual([c.args[0] for c in command.call_args_list],
                         [['make', '-C', str(self.kernel)]])

    def test_old_binary_is_built_before_reload_and_removal_race_stops_install(self):
        def old_modinfo(argv, **kwargs):
            return os.uname().release if argv[2] == 'vermagic' else ''

        def command(argv, **kwargs):
            if argv[0] == 'rmmod':
                raise subprocess.CalledProcessError(1, argv)

        with patch('scheduler.installer.subprocess.check_output', side_effect=old_modinfo), \
             patch('scheduler.installer.shutil.which', return_value='/usr/bin/gcc-12'), \
             patch('scheduler.installer.subprocess.run', side_effect=command) as execute, \
             patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaises(subprocess.CalledProcessError):
                ensure_module(5001)
        self.assertEqual([c.args[0] for c in execute.call_args_list],
                         [['make', '-C', str(self.kernel)], ['rmmod', 'sch_flowprio']])
