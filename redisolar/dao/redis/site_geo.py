from typing import Dict
from typing import List
from typing import Set

from redisolar.dao.base import SiteGeoDaoBase
from redisolar.dao.base import SiteNotFound
from redisolar.dao.redis.base import RedisDaoBase
from redisolar.models import GeoQuery
from redisolar.models import Site
from redisolar.schema import FlatSiteSchema

CAPACITY_THRESHOLD = 0.2


class SiteGeoDaoRedis(SiteGeoDaoBase, RedisDaoBase):
    """SiteGeoDaoRedis persists and queries Sites in Redis."""
    def insert(self, site: Site, **kwargs):
        """Insert a Site into Redis."""
        hash_key = self.key_schema.site_hash_key(site.id)
        client = kwargs.get('pipeline', self.redis)
        client.hset(hash_key, mapping=FlatSiteSchema().dump(site))  # type: ignore

        if not site.coordinate:
            raise ValueError("Site coordinates are required for Geo insert")

        client.geoadd(  # type: ignore
            self.key_schema.site_geo_key(), site.coordinate.lng, site.coordinate.lat,
            site.id)

    def insert_many(self, *sites: Site, **kwargs) -> None:
        """Insert multiple Sites into Redis."""
        for site in sites:
            self.insert(site, **kwargs)

    def find_by_id(self, site_id: int, **kwargs) -> Site:
        """Find a Site by ID in Redis."""
        hash_key = self.key_schema.site_hash_key(site_id)
        site_hash = self.redis.hgetall(hash_key)

        if not site_hash:
            raise SiteNotFound()

        return FlatSiteSchema().load(site_hash)

    def _find_by_geo(self, query: GeoQuery, **kwargs) -> Set[Site]:
        site_ids = self.redis.georadius(  # type: ignore
            self.key_schema.site_geo_key(), query.coordinate.lng, query.coordinate.lat,
            query.radius, query.radius_unit.value)
        sites = [
            self.redis.hgetall(self.key_schema.site_hash_key(site_id))
            for site_id in site_ids
        ]
        return {FlatSiteSchema().load(site) for site in sites}
    
    def _get_scores_for_sites(self, site_ids: List[str]) -> Dict[str, float]:
        """Find scores for sites"""
        p = self.redis.pipeline(transaction=False)

        for site_id in site_ids:
            site_key = self.key_schema.capacity_ranking_key()
            p.zscore(site_key, site_id)
        
        scores_for_sites = p.execute()
        return { site_ids[index]: score for index, score in enumerate(scores_for_sites) }

    def _find_by_geo_with_capacity(self, query: GeoQuery, **kwargs) -> Set[Site]:

        geo_key = self.key_schema.site_geo_key()

        site_ids: List[str] = self.redis.georadius(
            geo_key,
            longitude=query.coordinate.lng,
            latitude=query.coordinate.lat,
            radius=query.radius,
            unit=query.radius_unit.value
        )

        scores = self._get_scores_for_sites(site_ids)
        p = self.redis.pipeline(transaction=False)

        for site_id in site_ids:
            if scores[site_id] and scores[site_id] > CAPACITY_THRESHOLD:
                p.hgetall(self.key_schema.site_hash_key(site_id))
        site_hashes = p.execute()

        return {FlatSiteSchema().load(site) for site in site_hashes}

    def find_by_geo(self, query: GeoQuery, **kwargs) -> Set[Site]:
        """Find Sites using a geographic query."""
        if query.only_excess_capacity:
            return self._find_by_geo_with_capacity(query)
        return self._find_by_geo(query)

    def find_all(self, **kwargs) -> Set[Site]:
        """Find all Sites."""
        site_ids = self.redis.zrange(self.key_schema.site_geo_key(), 0, -1)
        sites = set()

        pipeline = self.redis.pipeline(transaction=False)

        for site_id in site_ids:
            key = self.key_schema.site_hash_key(site_id)
            site_hash = pipeline.hgetall(key)
        
        site_hashes = pipeline.execute()
        
        for site_hash in site_hashes:
            sites.add(FlatSiteSchema().load(site_hash))

        return sites
