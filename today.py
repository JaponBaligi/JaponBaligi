import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']  # 'JaponBaligi'
QUERY_COUNT = {'user_getter': 0, 'loc_query': 0, 'recursive_loc': 0}

GRAPHQL_URL = 'https://api.github.com/graphql'
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def graphql(func_name, query, variables):
    """
    POST a GraphQL query and return the parsed JSON body.
    """
    request = SESSION.post(GRAPHQL_URL, json={'query': query, 'variables': variables}, timeout=60)
    if request.status_code == 200:
        return request.json()
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def cache_filename():
    return 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'


def hashed_repo(name):
    return hashlib.sha256(name.encode('utf-8')).hexdigest()


def repo_node(edge):
    return ((edge or {}).get('node')) or {}


def repo_connection(body):
    """
    Returns (edges, pageInfo, totalCount) from a repositories GraphQL payload.
    Drops edges whose node is null (inaccessible, deleted, or partial GraphQL errors).
    """
    repos = (((body.get('data') or {}).get('user') or {}).get('repositories')) or {}
    edges = [edge for edge in (repos.get('edges') or []) if repo_node(edge)]
    page = repos.get('pageInfo') or {}
    return edges, page, repos.get('totalCount') or 0


def user_getter(username):
    """
    Returns the account ID, creation time, and follower count of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!) {
        user(login: $login) {
            id
            createdAt
            followers {
                totalCount
            }
        }
    }'''
    user = (graphql(user_getter.__name__, query, {'login': username}).get('data') or {}).get('user') or {}
    return {'id': user.get('id')}, user.get('createdAt'), int((user.get('followers') or {}).get('totalCount') or 0)


def fetch_repo_edges(owner_affiliation):
    """
    One paginated query for repo names, HEAD oids, stars, and owners.
    oid is used for cache invalidation instead of history.totalCount (which is slow).
    """
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazerCount
                            owner {
                                login
                            }
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        oid
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    cursor = None
    edges = []
    total_count = 0
    while True:
        query_count('loc_query')
        body = graphql('fetch_repo_edges', query, {
            'owner_affiliation': owner_affiliation,
            'login': USER_NAME,
            'cursor': cursor,
        })
        page_edges, page, total_count = repo_connection(body)
        edges.extend(page_edges)
        if not page.get('hasNextPage'):
            return edges, total_count
        cursor = page.get('endCursor')


def owned_repo_stats(edges):
    """
    Stars and repo count for repositories owned by USER_NAME.
    """
    stars = 0
    owned = 0
    user = USER_NAME.lower()
    for edge in edges:
        node = repo_node(edge)
        owner = ((node.get('owner') or {}).get('login') or '')
        if owner.lower() == user:
            owned += 1
            stars += node.get('stargazerCount') or 0
    return stars, owned


def recursive_loc(owner, repo_name, data, cache_comment):
    """
    Walk commit history 100 at a time and count LOC authored by me.
    """
    addition_total = 0
    deletion_total = 0
    my_commits = 0
    cursor = None
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    while True:
        query_count('recursive_loc')
        request = SESSION.post(
            GRAPHQL_URL,
            json={'query': query, 'variables': {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}},
            timeout=60,
        )
        if request.status_code != 200:
            force_close_file(data, cache_comment)
            if request.status_code == 403:
                raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
            raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)

        repo = ((request.json().get('data') or {}).get('repository')) or {}
        ref = repo.get('defaultBranchRef')
        if not ref:
            return addition_total, deletion_total, my_commits
        history = ((ref.get('target') or {}).get('history')) or {}
        for edge in history.get('edges') or []:
            node = (edge or {}).get('node') or {}
            if (node.get('author') or {}).get('user') == OWNER_ID:
                my_commits += 1
                addition_total += node.get('additions') or 0
                deletion_total += node.get('deletions') or 0
        page = history.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            return addition_total, deletion_total, my_commits
        cursor = page.get('endCursor')


def loc_query(owner_affiliation, comment_size=0, force_cache=False):
    """
    List repositories, refresh LOC cache for repos whose HEAD oid changed,
    and return loc totals plus derived star/repo/commit counts.
    """
    edges, contrib_count = fetch_repo_edges(owner_affiliation)
    print(f"Number of edges: {len(edges)}")
    loc_data, commit_data, cached = cache_builder(edges, comment_size, force_cache)
    star_data, repo_data = owned_repo_stats(edges)
    return loc_data + [cached], contrib_count, star_data, repo_data, commit_data


def head_oid(node):
    ref = node.get('defaultBranchRef') or {}
    target = ref.get('target') or {}
    return target.get('oid') or ''


def cache_builder(edges, comment_size, force_cache):
    """
    Hash-keyed cache. A repo is recounted only when its HEAD oid changes,
    or when it is new. Adding/removing a repo no longer flushes every row.
    Old numeric commit-count rows are migrated to oid without a full recount.
    """
    filename = cache_filename()
    try:
        with open(filename, 'r') as f:
            raw = f.readlines()
    except FileNotFoundError:
        raw = ['This line is a comment block. Write whatever you want here.\n'] * comment_size
        with open(filename, 'w') as f:
            f.writelines(raw)

    cache_comment = raw[:comment_size]
    records = {}
    for line in raw[comment_size:]:
        parts = line.split()
        if len(parts) >= 5:
            records[parts[0]] = parts

    recounted = False
    output_lines = []
    loc_add = 0
    loc_del = 0
    commit_total = 0

    for edge in edges:
        node = repo_node(edge)
        name = node.get('nameWithOwner')
        if not name:
            continue
        repo_key = hashed_repo(name)
        oid = head_oid(node)
        parts = records.get(repo_key)

        if not oid:
            parts = [repo_key, '0', '0', '0', '0']
        elif parts and not force_cache and parts[1] == oid:
            pass
        elif parts and not force_cache and parts[1].isdigit():
            # Previous cache stored commit counts; keep LOC and pin the current HEAD.
            parts = [repo_key, oid, parts[2], parts[3], parts[4]]
        else:
            recounted = True
            owner, repo_name = name.split('/', 1)
            snapshot = [' '.join(row[:5]) + '\n' for row in records.values()]
            loc = recursive_loc(owner, repo_name, snapshot, cache_comment)
            parts = [repo_key, oid, str(loc[2]), str(loc[0]), str(loc[1])]

        records[repo_key] = parts
        output_lines.append(' '.join(parts[:5]) + '\n')
        loc_add += int(parts[3])
        loc_del += int(parts[4])
        commit_total += int(parts[2])

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(output_lines)

    print(f"Number of data lines: {len(output_lines)}")
    return [loc_add, loc_del, loc_add - loc_del], commit_total, not recounted


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data) - 3]
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)

    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if my_commits.isdigit():
            added_commits += int(my_commits)

    if old_data:
        last_line = old_data[-1].split()
        if len(last_line) > 4:
            added_commits += int(last_line[4][:-1])
        else:
            print("Warning: Last line in archived data is malformed or missing expected data.")
    else:
        print("Warning: No data in repository archive.")

    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it.
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed.
    """
    filename = cache_filename()
    try:
        if data or cache_comment:
            with open(filename, 'w') as f:
                f.writelines(cache_comment)
                f.writelines(data)
            print(f"There was an error while writing to the cache file. The file, {filename}, has had the partial data saved and closed.")
            return True
        print("Warning: No data to write to the cache file.")
        return False
    except Exception as e:
        print(f"Error while saving the cache file: {e}")
        return False


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    # field_width is len(dots)+len(value) so each stats column stays a fixed character width
    justify_format(root, 'age_data', age_data, 51)          # '. Uptime:' + 51 = 60
    justify_format(root, 'repo_data', repo_data, 9)          # '. Repos:' + 9 + contrib block + ' ' = 36
    contrib_text = f"{contrib_data:,}" if isinstance(contrib_data, int) else str(contrib_data)
    find_and_replace(root, 'contrib_data', contrib_text.rjust(2))
    justify_format(root, 'star_data', star_data, 16)         # 'Stars:' + 16 = 22
    justify_format(root, 'commit_data', commit_data, 25)     # '. Commits:' + 25 = 35
    justify_format(root, 'follower_data', follower_data, 12) # 'Followers:' + 12 = 22

    loc_add, loc_del, loc_net = str(loc_data[0]), str(loc_data[1]), str(loc_data[2])
    find_and_replace(root, 'loc_add', loc_add)
    find_and_replace(root, 'loc_del', loc_del)
    find_and_replace(root, 'loc_data', loc_net)
    find_and_replace(root, 'loc_data_dots', dot_pad(25 - len(loc_net) - len(loc_add) - len(loc_del)))
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def dot_pad(fill_len):
    """
    Return a dots string of exactly fill_len characters: ' ... '.
    """
    if fill_len <= 0:
        return ''
    if fill_len == 1:
        return ' '
    if fill_len == 2:
        return '. '
    return ' ' + ('.' * (fill_len - 2)) + ' '


def justify_format(root, element_id, new_text, field_width=0):
    """
    Updates element text and pads the matching *_dots tspan so dots+value equal field_width.
    """
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    if field_width:
        find_and_replace(root, f"{element_id}_dots", dot_pad(field_width - len(new_text)))


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    print('Calculation times:')
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date, follower_data = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2002, 1, 23))
    print(f"Age Data: {age_data}")
    formatter('age calculation', age_time)
    stats, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    total_loc, contrib_data, star_data, repo_data, commit_data = stats
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)

    if OWNER_ID == {'id': 'U_kgDOBaaC9g'}:  # only calculate for user JaponBaligi
        archived_data = add_archive()
        for index in range(len(total_loc) - 1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    for index in range(len(total_loc) - 1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    print('{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time)), ' s ', sep='')
    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
