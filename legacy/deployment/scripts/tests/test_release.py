"""Distribution contract/security tests. GitHub mutation/download calls are mocked."""
from argparse import Namespace
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from airgap.scripts import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        (self.source/'binary').write_bytes(b'unit artifact, not a Linux executable')
        self.manifest={'platform':'linux/arm64','bundle_version':'unit','runtime':{},'files':{'k3s':{'path':'binary'}}}
        (self.source/'bundle-manifest.json').write_text(json.dumps(self.manifest))
        self.pin=release.sha256(self.source/'bundle-manifest.json');(self.source/'manifest.sha256').write_text(self.pin+'\n')
        self.bundle={'root':str(self.source),'manifest':self.manifest,'manifest_sha256':self.pin}
        self.directory=self.root/'dist'
        with patch.object(release,'verify',return_value=self.bundle):self.packed=release.pack(self.source,self.directory,'v0.1.0',self.pin)
    def tearDown(self):self.temp.cleanup()
    def args(self,**kwargs):
        data=dict(directory=str(self.directory),architecture='arm64',version='v0.1.0',repo='team/repo',create_draft=False,target=None,notes_file=None)
        data.update(kwargs);return Namespace(**data)
    def test_both_architecture_names_and_invalid_version(self):
        for arch in ('arm64','amd64'):self.assertIn(f'railshot-airgap-{arch}-v0.1.0.tar.zst',release.names(arch,'v0.1.0'))
        with self.assertRaises(release.BundleError):release.names('arm64','latest')
    def test_local_compression_roundtrip_excludes_unlisted_files(self):
        (self.source/'secret-extra').write_text('not in manifest')
        archive=self.directory/release.names('arm64','v0.1.0')[0]
        out=self.root/'unpacked';release.unpack(archive,out)
        self.assertEqual((out/'binary').read_bytes(),(self.source/'binary').read_bytes())
        self.assertFalse((out/'secret-extra').exists())
        self.assertEqual(release.sha256(out/'bundle-manifest.json'),self.pin)
    def test_same_verified_bundle_reused_and_different_manifest_refused(self):
        with patch.object(release,'verify',return_value=self.bundle):self.assertEqual(release.pack(self.source,self.directory,'v0.1.0')['status'],'reused')
        other={**self.bundle,'manifest_sha256':'0'*64}
        with patch.object(release,'verify',return_value=other),self.assertRaises(release.BundleError) as e:release.pack(self.source,self.directory,'v0.1.0')
        self.assertEqual(e.exception.code,'ARTIFACT_IMMUTABLE')
    def test_corrupt_release_archive_rejected(self):
        archive=self.directory/release.names('arm64','v0.1.0')[0];archive.write_bytes(b'corrupt')
        with self.assertRaises(release.BundleError) as e:release.validate_assets(self.directory,'arm64','v0.1.0')
        self.assertEqual(e.exception.code,'ARTIFACT_CHECKSUM_MISMATCH')
    def test_traversal_archive_rejected(self):
        raw=self.root/'unsafe.tar'
        with tarfile.open(raw,'w') as tar:
            member=tarfile.TarInfo('../outside');member.size=3;tar.addfile(member,io.BytesIO(b'bad'))
        subprocess.run(['zstd','-q',str(raw),'-o',str(self.root/'unsafe.zst')],check=True)
        with self.assertRaises(release.BundleError):release.unpack(self.root/'unsafe.zst',self.root/'extracted')
        self.assertFalse((self.root/'outside').exists())
    def test_upload_draft_only_and_no_overwrite(self):
        for state,code in [({'isDraft':False,'assets':[]},'RELEASE_IMMUTABLE'),({'isDraft':True,'assets':[{'name':release.names('arm64','v0.1.0')[0]}]},'RELEASE_ASSET_EXISTS')]:
            with patch.object(release,'auth'),patch.object(release.subprocess,'run',return_value=MagicMock(returncode=0,stdout=json.dumps(state))),patch.object(release,'call') as call,self.assertRaises(release.BundleError) as e:
                release.upload(self.args())
            self.assertEqual(e.exception.code,code);call.assert_not_called()
    def test_upload_explicit_repo_and_assets_never_clobbers(self):
        with patch.object(release,'auth') as auth,patch.object(release.subprocess,'run',return_value=MagicMock(returncode=0,stdout='{"isDraft":true,"assets":[]}')),patch.object(release,'call') as call:
            result=release.upload(self.args())
        auth.assert_called_once_with('team/repo',write=True)
        command=call.call_args.args[0];self.assertNotIn('--clobber',command);self.assertFalse(result['published'])
        self.assertEqual(command[3],'airgap-bundle-v0.1.0')
    def test_github_write_permission_required(self):
        with patch.object(release,'call',side_effect=['',json.dumps({'permissions':{'push':False}})]),self.assertRaises(release.BundleError) as e:release.auth('team/repo',True)
        self.assertEqual(e.exception.code,'RELEASE_PERMISSION_DENIED')
    def test_download_requires_trusted_archive_and_manifest_pins(self):
        args=self.args(output=str(self.root/'download'),archive_sha256='0'*64,manifest_sha256=self.pin)
        def fetch(command,timeout):
            import shutil
            target=Path(command[command.index('--dir')+1]);
            for f in self.directory.iterdir():shutil.copyfile(f,target/f.name)
            self.assertIn('airgap-bundle-v0.1.0',command);self.assertNotIn('latest',command)
        with patch.object(release,'auth'),patch.object(release,'call',side_effect=fetch),self.assertRaises(release.BundleError) as e:release.download(args)
        self.assertEqual(e.exception.code,'ARTIFACT_CHECKSUM_MISMATCH');self.assertFalse(Path(args.output).exists())


if __name__=='__main__':unittest.main()
