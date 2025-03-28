#!/usr/bin/env python

import argparse
import datetime
import boto3
import pytz
import sys


def save_to_s3(project, data):
    table_content = ""
    client = boto3.client("s3")
    for repo, tag, window, age, pushed in data:
        table_content += f"<tr><td>{repo}</td><td>{tag}</td><td>{window}</td><td>{age}</td><td>{pushed}</td></tr>"
    html_body = f"""
    <html>
        <head>
            <link rel="stylesheet"
                href="https://stackpath.bootstrapcdn.com/bootstrap/4.4.1/css/bootstrap.min.css"
                integrity="sha384-Vkoo8x4CGsO3+Hhxv8T/Q5PaXtkKtu6ug5TOeNV6gBiFeWPGFN9MuhOf23Q9Ifjh"
                crossorigin="anonymous">
            <link rel="stylesheet" type="text/css" href="https://cdn.datatables.net/1.10.20/css/jquery.dataTables.css">
            <script src="https://ajax.googleapis.com/ajax/libs/jquery/3.4.1/jquery.min.js"></script>
            <script type="text/javascript" charset="utf8" src="https://cdn.datatables.net/1.10.20/js/jquery.dataTables.js"></script>
            <title>{project} nightly and permanent docker image info</title>
        </head>
        <body>
            <table class="table table-striped table-hover" id="docker">
            <thead class="thead-dark">
                <tr>
                <th scope="col">repo</th>
                <th scope="col">tag</th>
                <th scope="col">keep window</th>
                <th scope="col">age</th>
                <th scope="col">pushed at</th>
                </tr>
            </thead>
            <tbody>
                {table_content}
            </tbody>
            </table>
        </body>
        <script>
            $(document).ready( function () {{
                $('#docker').DataTable({{paging: false}});
            }} );
        </script>
    </html>
    """

    # for pytorch, file can be found at
    # http://ossci-docker.s3-website.us-east-1.amazonaws.com/pytorch.html
    # and later one we can config docker.pytorch.org to point to the location

    client.put_object(
        Bucket="ossci-docker",
        ACL="public-read",
        Key=f"{project}.html",
        Body=html_body,
        ContentType="text/html",
    )


def repos(client):
    paginator = client.get_paginator("describe_repositories")
    pages = paginator.paginate(registryId="308535385114")
    for page in pages:
        for repo in page["repositories"]:
            yield repo


def images(client, repository):
    paginator = client.get_paginator("describe_images")
    pages = paginator.paginate(
        registryId="308535385114", repositoryName=repository["repositoryName"]
    )
    for page in pages:
        for image in page["imageDetails"]:
            yield image


parser = argparse.ArgumentParser(description="Delete old Docker tags from registry")
parser.add_argument(
    "--dry-run", action="store_true", help="Dry run; print tags that would be deleted"
)
parser.add_argument(
    "--keep-stable-days",
    type=int,
    default=14,
    help="Days of stable Docker tags to keep (non per-build images)",
)
parser.add_argument(
    "--keep-unstable-days",
    type=int,
    default=1,
    help="Days of unstable Docker tags to keep (per-build images)",
)
parser.add_argument(
    "--filter-prefix",
    type=str,
    default="",
    help="Only run cleanup for repositories with this prefix",
)
parser.add_argument(
    "--ignore-tags",
    type=str,
    default="",
    help="Never cleanup these tags (comma separated)",
)
args = parser.parse_args()

if not args.ignore_tags or not args.filter_prefix:
    print(
        """
Missing required arguments --ignore-tags and --filter-prefix

You must specify --ignore-tags and --filter-prefix to avoid accidentally
pruning a stable Docker tag which is being actively used.  This will
make you VERY SAD.  So pay attention.

First, which filter-prefix do you want?  The list of valid prefixes
is in jobs/private.groovy under the 'docker-registry-cleanup' job.
You probably want either pytorch or caffe2.

Second, which ignore-tags do you want?  It should be whatever the most
up-to-date DockerVersion for the repository in question is.  Follow
the imports of jobs/pytorch.groovy to find them.
"""
    )
    sys.exit(1)

client = boto3.client("ecr", region_name="us-east-1")
stable_window = datetime.timedelta(days=args.keep_stable_days)
unstable_window = datetime.timedelta(days=args.keep_unstable_days)
now = datetime.datetime.now(pytz.UTC)
ignore_tags = args.ignore_tags.split(",")


def chunks(chunkable, n):
    """ Yield successive n-sized chunks from l.
    """
    for i in range(0, len(chunkable), n):
        yield chunkable[i : i + n]


stable_window_tags = []
for repo in repos(client):
    repositoryName = repo["repositoryName"]
    if not repositoryName.startswith(args.filter_prefix):
        continue

    # Keep list of image digests to delete for this repository
    digest_to_delete = []
    print(repositoryName)

    for image in images(client, repo):
        tags = image.get("imageTags")
        if not isinstance(tags, (list,)) or len(tags) == 0:
            continue

        tag = tags[0]
        created = image["imagePushedAt"]
        age = now - created
        # new images build on circle ci use workflow ID as tag, which has 4 "-"
        if tag.isdigit() or tag.count("-") == 4 or tag in ignore_tags:
            window = stable_window
            if tag in ignore_tags:
                stable_window_tags.append((repositoryName, tag, "", age, created))
            elif age < window:
                stable_window_tags.append((repositoryName, tag, window, age, created))
        else:
            window = unstable_window

        print(
            f"Debug: for tag: {tag}, keep window is {window}, age is {age}, pushed at {image['imagePushedAt']}"
        )

        if tag in ignore_tags:
            print("Ignoring tag {} (age: {})".format(tag, age))
            continue
        if age < window:
            print("Not deleting manifest for tag {} (age: {})".format(tag, age))
            continue

        if args.dry_run:
            print("(dry run) Deleting manifest for tag {} (age: {})".format(tag, age))
        else:
            print("Deleting manifest for tag {} (age: {})".format(tag, age))
            digest_to_delete.append(image["imageDigest"])

    # Issue batch delete for all images to delete for this repository
    # Note that as of 2018-07-25, the maximum number of images you can
    # delete in a single batch is 100, so chunk our list into batches of
    # 100
    for c in chunks(digest_to_delete, 100):
        client.batch_delete_image(
            registryId="308535385114",
            repositoryName=repositoryName,
            imageIds=[{"imageDigest": digest} for digest in c],
        )

    save_to_s3(args.filter_prefix, stable_window_tags)
