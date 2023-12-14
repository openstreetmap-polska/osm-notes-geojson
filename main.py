import json
import re
from datetime import datetime
from math import ceil

import anyio
from httpx import AsyncClient
from shapely import box
from shapely.geometry import shape
from shapely.ops import BaseGeometry

HTTP = AsyncClient(
    base_url='https://api.openstreetmap.org/api/0.6/',
    follow_redirects=True,
    timeout=60,
)

API_LIMIT = 10_000

INIT_CELL_SIZE = 4


def _load_country_shape() -> BaseGeometry:
    with open('osm-countries-0-001.geojson', 'rb') as f:
        countries = json.load(f)

    countries = countries['features']
    country = next(c for c in countries if c['properties']['tags']['ISO3166-1'] == 'PL')
    return shape(country['geometry'])


def _save_result(features: list[dict]) -> None:
    geojson = {
        'type': 'FeatureCollection',
        'features': sorted(features, key=lambda f: f['properties']['id']),
    }

    filename = f'notes_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.geojson'

    with open(filename, 'w') as f:
        json.dump(geojson, f)


def process_feature(feature):
    first_comment: dict = feature['properties']['comments'][0]
    feature['properties']['user'] = first_comment.get('user', 'anonymous')
    feature['properties']['text'] = first_comment['text']
    feature['properties']['tags'] = ' '.join(set(re.findall(r'#\S+', first_comment['text'])))
    feature['properties']['year'] = int(first_comment['date'][:4])
    del feature['properties']['url']
    del feature['properties']['comment_url']
    del feature['properties']['close_url']
    del feature['properties']['comments']
    return feature


async def main():
    country = _load_country_shape()
    features = []

    async with anyio.create_task_group() as tg:

        async def process_cell(lon: float, lat: float, size: float) -> None:
            # skip cells that are outside of the country
            if not country.intersects(box(lon, lat, lon + size, lat + size)):
                return

            r = await HTTP.get(
                'notes.json',
                params={
                    'bbox': f'{lon},{lat},{lon + size},{lat + size}',
                    'limit': API_LIMIT,
                    'closed': 0,
                },
            )
            r.raise_for_status()

            cell_result = r.json()['features']

            if len(cell_result) == API_LIMIT:
                # if limit reached, split cell into 4 smaller cells
                new_size = size / 2
                tg.start_soon(process_cell, lon, lat, new_size)
                tg.start_soon(process_cell, lon + new_size, lat, new_size)
                tg.start_soon(process_cell, lon, lat + new_size, new_size)
                tg.start_soon(process_cell, lon + new_size, lat + new_size, new_size)
            else:
                # otherwise, filter and save cell results
                cell_result = tuple(
                    process_feature(feature)
                    for feature in cell_result
                    if feature['properties']['comments'] and country.contains(shape(feature['geometry']))
                )
                features.extend(cell_result)
                print(f'Cell ({lon:.6f}-{lon+size:.6f}, {lat:.6f}-{lat+size:.6f}): retrieved {len(cell_result)} notes')

        # bootstrap the process with the whole country grid
        min_lon, min_lat, max_lon, max_lat = country.bounds
        lon_steps = ceil((max_lon - min_lon) / INIT_CELL_SIZE)
        lat_steps = ceil((max_lat - min_lat) / INIT_CELL_SIZE)

        for lon in (min_lon + i * INIT_CELL_SIZE for i in range(lon_steps)):
            for lat in (min_lat + i * INIT_CELL_SIZE for i in range(lat_steps)):
                tg.start_soon(process_cell, lon, lat, INIT_CELL_SIZE)

    _save_result(features)


if __name__ == '__main__':
    anyio.run(main)
