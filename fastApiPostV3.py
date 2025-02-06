from fastapi import FastAPI, HTTPException
import aiohttp
import asyncio
import json
import urllib.parse
from pydantic import BaseModel
from asyncio import Semaphore
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins, might need to change this 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.ubereats.com/",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "x-csrf-token": "x",
    "Content-Type": "application/json"
}

# Pydantic models
class LocationRequest(BaseModel):
    address: str

class BogoStoresRequest(BaseModel):
    address: str

# Semaphore for limiting concurrent requests
semaphore = Semaphore(10)  # Limit to 10 concurrent requests


# Optimized location data fetching
async def get_location_data(session, address: str) -> dict:
    mapSearchURL = "https://www.ubereats.com/_p/api/mapsSearchV1"
    mapSearchPayload = json.dumps({"query": address})

    async with session.post(mapSearchURL, headers=headers, data=mapSearchPayload) as response:
        if response.status != 200:
            raise HTTPException(status_code=response.status, detail="Failed to fetch location data")
        map_data = await response.json()
        if len(map_data["data"]) < 1:
            raise HTTPException(status_code=404, detail="Please enter a valid location address")
    placeId = map_data["data"][0]["id"]
    deliverySearchURL = "https://www.ubereats.com/_p/api/getDeliveryLocationV1"
    deliverySearchPayload = json.dumps({
        "placeId": placeId,
        "provider": "here_places",
        "source": "manual_auto_complete"
    })

    async with session.post(deliverySearchURL, headers=headers, data=deliverySearchPayload) as response:
        if response.status != 200:
            raise HTTPException(status_code=response.status, detail="Failed to fetch delivery data")
        return await response.json()


# Optimized store data fetching
async def fetch_store_data(session, store_uuid, getStoreURL):
    getStorePayload = {
        "storeUuid": store_uuid,
        "diningMode": "DELIVERY",
        "time": {"asap": True},
        "cbType": "EATER_ENDORSED"
    }

    async with session.post(getStoreURL, headers=headers, json=getStorePayload) as response:
        response_json = await response.json()

    if response_json["status"] != "success":
        return 
    
    storeMetaData = {"bogoFoods": []}
    info = response_json["data"]
    
    storeMetaData.update({
        "title": info["title"],
        "heroImageUrls": info["heroImageUrls"],
        "location": info["location"],
        "etaRange": info["etaRange"],
        "rating": info["rating"],
        "categories": info["categories"]
    })

    for uuidKey, arrayItems in response_json['data']['catalogSectionsMap'].items():
        if arrayItems:
            for foodGroup in arrayItems:
                if "standardItemsPayload" in foodGroup["payload"]:
                    standardItemsPayload = foodGroup["payload"]["standardItemsPayload"]
                    if "title" in standardItemsPayload:
                        offerGroup = standardItemsPayload["title"]["text"]
                        if offerGroup in ["Buy 1, Get 1 Free", "Offers"]:
                            for foodItem in standardItemsPayload["catalogItems"]:
                                if "itemPromotion" in foodItem and "buyXGetYItemPromotion" in foodItem["itemPromotion"]:
                                    try: 
                                        bogoItem = {
                                            "title": foodItem["title"],
                                            "price": foodItem["price"],
                                            "priceTagline": foodItem["priceTagline"],
                                            "buyXGetYItemPromotion": foodItem["itemPromotion"]["buyXGetYItemPromotion"]
                                        }
                                    except:
                                        print("\nERROR creating bogoitem: {} \n".format(storeMetaData["title"]))
                                        print(foodItem)

                                    if "itemDescription" in foodItem:
                                        bogoItem["itemDescription"] = foodItem["itemDescription"]
                                    storeMetaData["bogoFoods"].append(bogoItem)
                        break

    return storeMetaData if storeMetaData["bogoFoods"] else None


# Asynchronous generator for paginated store fetch
async def fetch_all_stores(session, getAllStoresURL, storesPayload):
    offset = 0
    while True:
        async with semaphore:
            async with session.post(getAllStoresURL, headers=headers, json=storesPayload(offset)) as response:
                allStoresReponseJSON = await response.json()

        for store in allStoresReponseJSON["data"]["feedItems"]:
            storePayLoad = store["store"]["tracking"]["storePayload"]
            if "offerMetadata" in storePayLoad and ("offerTypeCount" in storePayLoad["offerMetadata"]):
                yield store["uuid"]

        pagination = allStoresReponseJSON["data"]["meta"]
        offset = pagination["offset"]

        if not pagination["hasMore"]:
            break


# Optimized function to fetch all BOGO stores and their data
async def get_all_bogo_stores(locationData: dict):
    json_string = json.dumps(locationData["data"])
    encoded_json = urllib.parse.quote(json_string)
    locationCookie = f'uev2.loc={encoded_json}'

    async with aiohttp.ClientSession(headers={**headers, "Cookie": locationCookie}) as session:
        getAllStoresURL = "https://www.ubereats.com/_p/api/allStoresV1"


        # "sortAndFilters": [
        #             {
        #                 "uuid": "33e0f7cc-8927-4dac-a92e-19a296aab097",
        #                 "options": [{"uuid": "g996476c-2b1b-4db2-b40a-13d43cb117dc"}]
        #             }
        #         ],
        def storesPayload(offsetNum):
            return {
                "date": "",
                "startTime": 0,
                "endTime": 0,
                "surfaceName": "HOME",
                "cacheKey": "",
                "verticalType": "",
                "pageInfo": {"offset": offsetNum, "pageSize": None}
            }

        store_uuids = []
        async for store_uuid in fetch_all_stores(session, getAllStoresURL, storesPayload):
            store_uuids.append(store_uuid)

        getStoreURL = "https://www.ubereats.com/_p/api/getStoreV1"

        # Batch store data fetching
        tasks = [fetch_store_data(session, store_uuid, getStoreURL) for store_uuid in store_uuids]
        results = await asyncio.gather(*tasks)

        return [result for result in results if result]

# FastAPI endpoints
@app.post("/location")
async def location(location: LocationRequest):
    async with aiohttp.ClientSession() as session:
        try:
            location_data = await get_location_data(session, location.address)
            return location_data
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/bogo-stores")
async def bogo_stores(location: BogoStoresRequest):
    if not location.address:
        return {}
    async with aiohttp.ClientSession() as session:
        location_data = await get_location_data(session, location.address)
        try:
            stores = await get_all_bogo_stores(location_data)
            return {"bogoStores": stores}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# async def main():
#     async with aiohttp.ClientSession() as session:
#         locationData = await get_location_data(session, "5407 Stearns Hill Rd, Waltham, MA")
#         await get_all_bogo_stores(locationData)


# Run FastAPI app
if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # Fix for Windows
    uvicorn.run(app, host="0.0.0.0", port=8000)
