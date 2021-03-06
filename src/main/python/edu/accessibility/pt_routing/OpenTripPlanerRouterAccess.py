import json

import pandas as pd
import requests
import time
from joblib import delayed, Parallel

from src.main.python.edu.accessibility.pt_routing.PostGISServiceProvider import PostGISServiceProvider
from src.main.python.edu.accessibility.util.utilitaries import dgl_timer, getConfigurationProperties, Logger, \
    parallel_job_print, Counter, timeDifference, getFormattedDatetime


def createEmptyTravelTimeDataFrame():
    return pd.DataFrame(columns=[
        "plan_index",
        "waiting_time",
        "walk_time",
        "transit_time",
        "duration",
        "transfers",
        "start_time",
        "end_time",
        "min_boardings",
        "max_boardings",
        "mean_boardings",
        "max_duration",
        "mean_duration",
        "mean_duration_max_boardings",
        "duration_standard_deviation"
    ])


def analyseOriginDestination(self, origin, destinationsDF, date, _time, worstTime, logger):
    startTime = time.time()
    functionName = "analyseOriginDestination"
    logger.info("%s Start Time: %s" % (functionName, getFormattedDatetime(timemilis=startTime)))

    fastestRoutes = createEmptyTravelTimeDataFrame()
    for index, destination in destinationsDF.iterrows():
        plan = self.getRoutePlan(
            origin=origin.geometry,
            destination=destination.geometry,
            time=_time,
            date=date,
            worstTime=worstTime
        )

        Counter.generalCounter += 1

        if "error" not in plan:
            fastestRoute = self.getFastestRoute(plan)

            fastestRoute["from_id"] = origin["ykr_id"]
            fastestRoute["to_id"] = destination["ykr_id"]

            # fastestRoute.to_sql(
            #     getConfigurationProperties(section="DATABASE_CONFIG")["travel_time_table_name"],
            #     self.postgresProvider.getEngine(),
            #     if_exists='append',
            #     index=False
            # )

            fastestRoutes = fastestRoutes.append(fastestRoute, ignore_index=True)

            Counter.processedCounter += 1

            logger.info("General: %s/%s (%s), Processed: %s/%s (%s)" % (
                Counter.generalCounter, Counter.maxPlansToProcess, Counter.getGeneralPercentage(),
                Counter.processedCounter, Counter.maxPlansToProcess, Counter.getProcessedPercentage()))
        else:
            Counter.errorsCounter += 1

            logger.exception(
                "OTP Error: %s: %s. (%s/%s, %s)" % (plan["error"]["message"], plan["error"]["msg"],
                                                    Counter.errorsCounter, Counter.maxPlansToProcess,
                                                    Counter.getErrorPercentage())
            )

    endTime = time.time()
    logger.info("%s End Time: %s" % (functionName, getFormattedDatetime(timemilis=endTime)))

    totalTime = timeDifference(startTime, endTime)
    logger.info("%s Total Time: %s m" % (functionName, totalTime))
    return fastestRoutes


class OpenTripPlanerRouterAccess:
    DURATION_SECONDS_DIVIDER = 60  # 60 Seconds

    def __init__(self):
        self.postgresProvider = PostGISServiceProvider()

    @dgl_timer
    def getRoutePlan(self, origin, destination, time, date, worstTime):
        options = {
            'fromPlace': '%s,%s' % (origin.y, origin.x),
            'toPlace': '%s,%s' % (destination.y, destination.x),
            'time': time,  # '1:02pm'
            'date': date,  # mm-dd-yyyy
            'mode': 'TRANSIT,WALK',
            'maxWalkDistance': 5000,
            'maxHours': 3,
            'worstTime': worstTime,
            'numItineraries': 100,
            "maxTransfers": 5,
            "walkSpeed": 1.16667,
            "arriveBy": False,
            "wheelchair": False,
            "locale": "en"
        }
        response = requests.get(
            "http://localhost:8080/otp/routers/default/plan",
            params=options
        )
        # parse from JSON to python dictionary
        response = json.loads(response.text)
        return response

    @dgl_timer
    def getFastestRoute(self, plan):

        itineraryDF = createEmptyTravelTimeDataFrame()
        count = 0
        for itinerary in plan["plan"]["itineraries"]:
            # if itinerary["endTime"] / 1000 < rushHourEndTime:
            itineraryDF.loc[len(itineraryDF)] = [
                count,
                int(itinerary["waitingTime"] / OpenTripPlanerRouterAccess.DURATION_SECONDS_DIVIDER),
                int(itinerary["walkTime"] / OpenTripPlanerRouterAccess.DURATION_SECONDS_DIVIDER),
                int(itinerary["transitTime"] / OpenTripPlanerRouterAccess.DURATION_SECONDS_DIVIDER),
                int(itinerary["duration"] / OpenTripPlanerRouterAccess.DURATION_SECONDS_DIVIDER),
                # itinerary["duration"],
                itinerary["transfers"],
                int(itinerary["startTime"] / 1000),
                int(itinerary["endTime"] / 1000),
                None,  # minBoardings
                None,  # maxBoardings
                None,  # meanBoardings
                None,  # maxDuration
                None,  # meanDuration
                None,  # meanDurationMaxBoardings
                None  # durationStandardDeviation
            ]
            count += 1

        itineraryDF = itineraryDF.sort_values(by=['start_time', 'end_time'], ascending=True)

        minDuration = min(itineraryDF["duration"])
        maxDuration = max(itineraryDF["duration"])
        fastestRoutes = itineraryDF[itineraryDF["duration"] == minDuration]
        fastestRoutes = fastestRoutes.sort_values(by=['transfers'], ascending=True)

        maxBoardings = max(itineraryDF["transfers"])
        minBoardings = min(itineraryDF["transfers"])
        fastestRoutes["min_boardings"] = minBoardings
        fastestRoutes["max_boardings"] = maxBoardings
        fastestRoutes["mean_boardings"] = sum(itineraryDF["transfers"]) / len(itineraryDF["transfers"])

        fastestRoutes["max_duration"] = maxDuration
        meanDuration = sum(itineraryDF["duration"]) / len(itineraryDF["duration"])
        fastestRoutes["mean_duration"] = meanDuration

        maxBoardingsRoutes = itineraryDF[itineraryDF["transfers"] == maxBoardings]
        # maxBoardingsRoutes = maxBoardingsRoutes.sort_values(by=['transfers'], ascending=False)
        fastestRoutes["mean_duration_max_boardings"] = sum(maxBoardingsRoutes["duration"]) / len(
            maxBoardingsRoutes["duration"])

        varianceOfDuration = sum((itineraryDF["duration"] - meanDuration) ** 2) / len(itineraryDF)
        standardDeviationDuration = varianceOfDuration ** (1 / 2)
        fastestRoutes["duration_standard_deviation"] = standardDeviationDuration

        return fastestRoutes.head(1)

    @dgl_timer
    def processPlans(self, originPointsDF, destinationsPointsDF, time, date, worstTime):
        delayedAnalyseOriginDestination = []

        Counter.maxPlansToProcess = len(originPointsDF) * len(destinationsPointsDF)

        for index, origin in originPointsDF.iterrows():
            # for index, destination in destinationsPointsDF.iterrows():
            #     destinationsSubsets[count]
            delayedAnalyseOriginDestination.append(
                delayed(analyseOriginDestination)(self, origin.copy(), destinationsPointsDF.copy(), date, time, worstTime,
                                                  Logger.getInstance()))

        with Parallel(n_jobs=int(getConfigurationProperties(section="PARALLELIZATION")["jobs"]),
                      backend="threading",
                      verbose=int(getConfigurationProperties(section="PARALLELIZATION")["verbose"])) as parallel:
            parallel._print = parallel_job_print
            returns = parallel(tuple(delayedAnalyseOriginDestination))

            for fastestRoutes in returns:
                fastestRoutes.to_sql(
                    getConfigurationProperties(section="DATABASE_CONFIG")["travel_time_table_name"],
                    self.postgresProvider.getEngine(),
                    if_exists='append',
                    index=False
                )
