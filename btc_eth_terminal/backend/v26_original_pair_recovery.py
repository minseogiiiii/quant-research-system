"""Explicit, bounded byte recovery from user-approved historical ZIP backups.

Only matching *_features_5m.csv.gz / *_future_labels.csv.gz members are copied.
No file is relabelled as Coinbase data, no features generated, no PIT is
certified, and no recovered source is automatically admitted to research.
"""
from __future__ import annotations

import argparse
import csv
import gzip
from hashlib import sha256
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
from zipfile import ZipFile, BadZipFile

SUFFIX = re.compile(r'^(?P<prefix>[A-Za-z0-9][A-Za-z0-9_.-]*)_(?P<kind>features_5m|future_labels)\.csv\.gz$', re.I)
MAX_ARCHIVES = 24
MAX_MEMBERS = 20_000
MAX_ORIGINAL_BYTES = 1_500_000_000
MAX_DECOMPRESSED_BYTES = 8_000_000_000
MAX_CSV_HEADER = 131_072


class RecoveryError(ValueError):
    pass


def checksum(path):
    digest=sha256()
    with Path(path).open('rb') as data:
        for block in iter(lambda: data.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def archive_member_name(info):
    raw=info.filename
    if (not isinstance(raw,str) or not raw or raw.startswith('/') or
            '\\' in raw or '\x00' in raw or
            any(piece in ('','.','..') for piece in raw.split('/')) or
            stat.S_ISLNK(info.external_attr >> 16)):
        raise RecoveryError('UNSAFE_ZIP_MEMBER_PATH_OR_SYMLINK')
    return PurePosixPath(raw).name


def gzip_header_and_full_crc(file):
    """Verify gzip framing/CRC to EOF and a CSV header, bounded decompression."""
    count=0
    with gzip.open(file,'rb') as source:
        header=source.readline(MAX_CSV_HEADER + 1)
        if len(header)>MAX_CSV_HEADER or not header.endswith(b'\n'):
            raise RecoveryError('MALFORMED_OR_OVERSIZED_CSV_HEADER')
        count+=len(header)
        try:
            header_names=next(csv.reader(io.StringIO(header.decode('utf-8-sig'),newline=''),strict=True))
        except (UnicodeError,csv.Error,StopIteration) as exc:
            raise RecoveryError('CSV_HEADER_ENCODING_INVALID') from exc
        if not header_names or any(not col.strip() or '\x00' in col for col in header_names):
            raise RecoveryError('CSV_HEADER_EMPTY_OR_AMBIGUOUS')
        if len(set(header_names)) != len(header_names):
            raise RecoveryError('CSV_DUPLICATE_COLUMNS')
        for block in iter(lambda: source.read(1024*1024),b''):
            count += len(block)
            if count > MAX_DECOMPRESSED_BYTES:
                raise RecoveryError('GZIP_EXPANSION_LIMIT_EXCEEDED')
    return header_names, count


def recover(archives, destination: Path):
    destination=Path(destination)
    if (destination.is_symlink() or destination.exists() or
            any(parent.is_symlink() for parent in destination.parents)):
        raise RecoveryError('DESTINATION_MUST_BE_NEW_DIRECTORY_WITHOUT_SYMLINK_PARENTS')
    if not archives or len(archives)>MAX_ARCHIVES:
        raise RecoveryError('INVALID_NUMBER_OF_ARCHIVES')
    inputs=[]
    for name in archives:
        path=Path(name)
        if path.is_symlink() or not path.is_file() or path.suffix.lower()!='.zip':
            raise RecoveryError('INVALID_ARCHIVE_INPUT')
        inputs.append(path.resolve())
    destination.mkdir(mode=0o700,parents=True)
    items=[]
    for archive in inputs:
        archive_sha=checksum(archive)
        try:
            with ZipFile(archive) as zf:
                infos=zf.infolist()
                if len(infos)>MAX_MEMBERS:
                    raise RecoveryError('EXCESSIVE_ZIP_MEMBERS')
                for member in infos:
                    if member.is_dir():
                        continue
                    basename=archive_member_name(member)
                    match=SUFFIX.fullmatch(basename)
                    if not match:
                        continue
                    if not 0<member.file_size<=MAX_ORIGINAL_BYTES:
                        raise RecoveryError('ZIP_ORIGINAL_SIZE_OUTSIDE_BOUNDS')
                    if (member.compress_size==0 or member.file_size/member.compress_size>5000):
                        raise RecoveryError('ZIP_MEMBER_SUSPICIOUS_EXPANSION')
                    target=destination/basename
                    # Never overwrite a different original with a colliding name.
                    if target.exists() or target.is_symlink():
                        raise RecoveryError('CONFLICTING_OR_DUPLICATE_ORIGINAL_NAME')
                    try:
                        count=0
                        with zf.open(member) as compressed, target.open('xb') as writer:
                            while True:
                                block=compressed.read(1024*1024)
                                if not block:break
                                count+=len(block)
                                if count>MAX_ORIGINAL_BYTES:
                                    raise RecoveryError('ZIP_STREAM_EXPANSION_LIMIT')
                                writer.write(block)
                        cols,plain_size=gzip_header_and_full_crc(target)
                    except BaseException:
                        target.unlink(missing_ok=True)
                        raise
                    items.append({'filename':basename,
                                  'source_zip_path':str(archive),
                                  'source_zip_sha256':archive_sha,
                                  'member_path':member.filename,
                                  'compressed_original_sha256':checksum(target),
                                  'compressed_original_bytes':count,
                                  'uncompressed_csv_bytes':plain_size,
                                  'header_columns':cols,
                                  'cohort_prefix_filename_only':match['prefix'],
                                  'kind':match['kind'].lower(),
                                  'source_provider_verified':False,
                                  'historical_pit_verified':False})
        except (BadZipFile, OSError, EOFError, gzip.BadGzipFile) as exc:
            raise RecoveryError('ZIP_OR_GZIP_INTEGRITY_FAILURE') from exc
        if checksum(archive) != archive_sha:
            raise RecoveryError('ORIGINAL_ARCHIVE_CHANGED_DURING_RECOVERY')
    by_cohort={}
    for item in items:
        by_cohort.setdefault(item['cohort_prefix_filename_only'].casefold(),set()).add(item['kind'])
    pairs=sorted(k for k,kinds in by_cohort.items() if kinds=={'features_5m','future_labels'})
    result={'schema':'v26_original_pair_byte_recovery_v1',
            'status':'RECOVERED_BYTES_NOT_PIT_ATTESTED' if items else 'NO_MATCHING_ORIGINAL_BYTES',
            'matching_originals':items,
            'filename_cohorts_with_both_originals_not_pit_verified':pairs,
            'source_provider_verified':False,
            'point_in_time_verified':False,
            'research_admission_allowed':False}
    report=destination/'V26_RECOVERY_REPORT.json'
    with report.open('x',encoding='utf8') as fd:
        json.dump(result,fd,indent=2,sort_keys=True);fd.write('\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives',nargs='+',type=Path)
    parser.add_argument('--destination',required=True,type=Path)
    args=parser.parse_args()
    out=recover(args.archives,args.destination)
    print(out['status'],len(out['matching_originals']))


if __name__=='__main__':
    main()
