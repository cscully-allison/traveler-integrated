import os
import re
import diskcache #pylint: disable=import-error
from blist import sortedlist #pylint: disable=import-error
from intervaltree import Interval, IntervalTree #pylint: disable=import-error
from .loggers import logToConsole

# Tools for handling OTF2 traces
eventLineParser = re.compile(r'^(\S+)\s+(\d+)\s+(\d+)\s+(.*)$')
attrParsers = {
    'ENTER': r'(Region): "([^"]*)"',
    'LEAVE': r'(Region): "([^"]*)"'
}
addAttrLineParser = re.compile(r'^\s+ADDITIONAL ATTRIBUTES: (.*)$')
addAttrSplitter = re.compile(r'\), \(')
addAttrParser = re.compile(r'\(?"([^"]*)" <\d+>; [^;]*; ([^\)]*)')

metricLineParser = re.compile(r'^METRIC\s+(\d+)\s+(\d+)\s+Metric:[\s\d,]+Value: \("([^"]*)" <\d+>; [^;]*; ([^\)]*)')
memInfoMetricParser = re.compile(r'^METRIC\s+(\d+)\s+(\d+)\s+Metric:[\s\d,]+Value: \("meminfo:([^"]*)" <\d+>; [^;]*; ([^\)]*)')

def processEvent(self, label, event, eventId):
    newR = seenR = 0

    if 'Region' in event:
        # Identify the primitive (and add to its counter)
        primitiveName = event['Region'].replace('::eval', '')
        event['Primitive'] = primitiveName
        del event['Region']
        primitive, newR = self.processPrimitive(label, primitiveName, 'otf2')
        seenR = 1 if newR == 0 else 0
        if self.debugSources is True:
            if 'eventCount' not in primitive:
                primitive['eventCount'] = 0
            primitive['eventCount'] += 1
        self.datasets[label]['primitives'][primitiveName] = primitive
    # Add enter / leave events to per-location lists
    if event['Event'] == 'ENTER' or event['Event'] == 'LEAVE':
        if not event['Location'] in self.sortedEventsByLocation:
            # TODO: use BPlusTree instead of blist? For big enough runs, piling
            # all this up in memory could be a problem...
            self.sortedEventsByLocation[event['Location']] = sortedlist(key=lambda i: i[0])
        self.sortedEventsByLocation[event['Location']].add((event['Timestamp'], event))
    # Store the event, if we're collecting them:
    if self.datasets[label]['meta']['storedEvents']:
        self.datasets[label]['events'][eventId] = event
    return (newR, seenR)

async def processOtf2(self, label, file, storeEvents=False, log=logToConsole):
    self.addSourceFile(label, file.name, 'otf2')

    # Set up database files
    labelDir = os.path.join(self.dbDir, label)
    primitives = self.datasets[label]['primitives']
    intervals = self.datasets[label]['intervals'] = diskcache.Index(os.path.join(labelDir, 'intervals.diskCacheIndex'))
    intervalIndexes = self.datasets[label]['intervalIndexes'] = {
        'primitives': {},
        'locations': {},
        'both': {}
    }
    procMetrics = self.datasets[label]['procMetrics'] = diskcache.Index(os.path.join(labelDir, 'procMetrics.diskCacheIndex'))
    guids = self.datasets[label]['guids'] = diskcache.Index(os.path.join(labelDir, 'guids.diskCacheIndex'))
    self.datasets[label]['meta']['storedEvents'] = storeEvents
    if storeEvents:
        self.datasets[label]['events'] = diskcache.Index(os.path.join(labelDir, 'events.diskCacheIndex'))

    # Temporary counters / lists for sorting
    numEvents = 0
    self.sortedEventsByLocation = {}
    await log('Parsing OTF2 events (.=2500 events)')
    newR = seenR = 0
    currentEvent = None
    includedMetrics = 0
    skippedMetricsForMissingPrior = 0
    skippedMetricsForMismatch = 0

    async for line in file:
        eventLineMatch = eventLineParser.match(line)
        addAttrLineMatch = addAttrLineParser.match(line)
        metricLineMatch = metricLineParser.match(line)
        if currentEvent is None and eventLineMatch is None and metricLineMatch is None:
            # This is a blank / header line
            continue

        if metricLineMatch is not None:
            # This is a metric line
            location = metricLineMatch.group(1)
            timestamp = int(metricLineMatch.group(2))
            metricType = metricLineMatch.group(3)
            value = int(float(metricLineMatch.group(4)))

            if metricType.startswith('PAPI'):
                if currentEvent is None:
                    skippedMetricsForMissingPrior += 1
                elif currentEvent['Timestamp'] != timestamp or currentEvent['Location'] != location: #pylint: disable=unsubscriptable-object
                    skippedMetricsForMismatch += 1
                else:
                    includedMetrics += 1
                    currentEvent['metrics'][metricType] = value #pylint: disable=unsubscriptable-object
            else: # do the other meminfo status io parsing here
                if metricType not in procMetrics:
                    procMetrics[metricType] = {}
                    if 'procMetricList' not in procMetrics:
                        procMetrics['procMetricList'] = []
                    pm = procMetrics['procMetricList']
                    pm.append(metricType)
                    procMetrics['procMetricList'] = pm
                val = procMetrics[metricType]
                val[str(timestamp)] = {'Timestamp': timestamp, 'Value':  value}
                procMetrics[metricType] = val
        elif eventLineMatch is not None:
            # This is the beginning of a new event; process the previous one
            if currentEvent is not None:
                counts = self.processEvent(label, currentEvent, str(numEvents))
                # Log that we've processed another event
                numEvents += 1
                if numEvents % 2500 == 0:
                    await log('.', end='')
                if numEvents % 100000 == 0:
                    await log('processed %i events' % numEvents)
                # Add to primitive / guid counts
                newR += counts[0]
                seenR += counts[1]
            currentEvent = {'metrics': {}}
            currentEvent['Event'] = eventLineMatch.group(1)
            currentEvent['Location'] = eventLineMatch.group(2)
            currentEvent['Timestamp'] = int(eventLineMatch.group(3))
            attrs = eventLineMatch.group(4)
            for attrMatch in re.finditer(attrParsers[currentEvent['Event']], attrs):
                currentEvent[attrMatch.group(1)] = attrMatch.group(2)
        else:
            # This line contains additional event attributes
            if currentEvent is None or addAttrLineMatch is None:
                print(currentEvent)
                print(addAttrLineMatch)
                print(line)
            assert currentEvent is not None and addAttrLineMatch is not None
            attrList = addAttrSplitter.split(addAttrLineMatch.group(1))
            for attrStr in attrList:
                attr = addAttrParser.match(attrStr)
                assert attr is not None
                currentEvent[attr.group(1)] = attr.group(2) #pylint: disable=unsupported-assignment-operation
    # The last event will never have had a chance to be processed:
    if currentEvent is not None:
        counts = self.processEvent(label, currentEvent, str(numEvents))
        newR += counts[0]
        seenR += counts[1]
    await log('')
    await log('Finished processing %i events' % numEvents)
    await log('New primitives: %d, References to existing primitives: %d' % (newR, seenR))
    await log('Metrics included: %d; skpped for no prior ENTER: %d; skipped for mismatch: %d' % (includedMetrics, skippedMetricsForMissingPrior, skippedMetricsForMismatch))

    # Now that we've seen all the locations, store that list in our metadata
    locationNames = self.datasets[label]['meta']['locationNames'] = sorted(self.sortedEventsByLocation.keys())

    # Combine the sorted enter / leave events into intervals
    await log('Combining enter / leave events into intervals (.=2500 intervals)')
    numIntervals = mismatchedIntervals = 0
    for location, eventList in self.sortedEventsByLocation.items():
        lastEvent = None
        for _, event in eventList:
            assert event is not None
            if event['Event'] == 'ENTER':
                # Start an interval (don't output anything)
                if lastEvent is not None:
                    # TODO: factorial data used to trigger this... why?
                    await log('WARNING: omitting ENTER event without a following LEAVE event (%s)' % lastEvent['name']) #pylint: disable=unsubscriptable-object
                lastEvent = event
            elif event['Event'] == 'LEAVE':
                # Finish a interval
                if lastEvent is None:
                    # TODO: factorial data used to trigger this... why?
                    await log('WARNING: omitting LEAVE event without a prior ENTER event (%s)' % event['name'])
                    continue
                intervalId = str(numIntervals)
                currentInterval = {'enter': {}, 'leave': {}, 'intervalId': intervalId}
                # Copy all of the attributes from the OTF2 events into the interval object. If the values
                # differ (or it's the timestamp), put them in nested enter / leave objects. Otherwise, put
                # them directly in the interval object
                for attr in set(event.keys()).union(lastEvent.keys()):
                    if attr not in event:
                        currentInterval['enter'][attr] = lastEvent[attr] #pylint: disable=unsubscriptable-object
                    elif attr not in lastEvent: #pylint: disable=E1135
                        currentInterval['leave'][attr] = event[attr]
                    elif attr != 'Timestamp' and event[attr] == lastEvent[attr]: #pylint: disable=unsubscriptable-object
                        currentInterval[attr] = event[attr]
                    else:
                        currentInterval['enter'][attr] = lastEvent[attr] #pylint: disable=unsubscriptable-object
                        currentInterval['leave'][attr] = event[attr]
                # Count whether the primitive attribute differed between enter / leave
                if 'Primitive' not in currentInterval:
                    mismatchedIntervals += 1
                intervals[intervalId] = currentInterval

                # Log that we've finished the finished interval
                numIntervals += 1
                if numIntervals % 2500 == 0:
                    await log('.', end='')
                if numIntervals % 100000 == 0:
                    await log('processed %i intervals' % numIntervals)
                lastEvent = None
        # Make sure there are no trailing ENTER events
        if lastEvent is not None:
            # TODO: fibonacci data triggers this... why?
            await log('WARNING: omitting trailing ENTER event (%s)' % lastEvent['Primitive'])
    del self.sortedEventsByLocation
    await log('')
    await log('Finished creating %i intervals; %i refer to mismatching primitives' % (numIntervals, mismatchedIntervals))

    # Now for indexing: we want per-location indexes, per-primitive indexes,
    # as well as both filters at the same time (we key by locations first)
    # TODO: these are all built in memory... should probably find a way to
    # make a diskcache-like version of IntervalTree:
    for location in locationNames:
        intervalIndexes['locations'][location] = IntervalTree()
        intervalIndexes['both'][location] = {}
    for primitive in primitives.keys():
        intervalIndexes['primitives'][primitive] = IntervalTree()
        for location in locationNames:
            intervalIndexes['both'][location][primitive] = IntervalTree()

    await log('Assembling interval indexes (.=2500 intervals)')
    count = 0
    async def intervalIterator():
        nonlocal count
        for intervalId, intervalObj in intervals.items():
            enter = intervalObj['enter']['Timestamp']
            leave = intervalObj['leave']['Timestamp'] + 1
            # Need to add one because IntervalTree can't handle zero-length intervals
            # (and because IntervalTree is not inclusive of upper bounds in queries)

            iv = Interval(enter, leave, intervalId)

            # Add the interval to the appropriate indexes (piggybacked off
            # the construction of the main index):
            location = intervalObj['Location']
            intervalIndexes['locations'][location].add(iv)
            if 'Primitive' in intervalObj:
                intervalIndexes['primitives'][intervalObj['Primitive']].add(iv)
                intervalIndexes['both'][location][intervalObj['Primitive']].add(iv)
            elif 'Primitive' in intervalObj['enter']:
                intervalIndexes['primitives'][intervalObj['enter']['Primitive']].add(iv)
                intervalIndexes['both'][location][intervalObj['enter']['Primitive']].add(iv)

            count += 1
            if count % 2500 == 0:
                await log('.', end='')
            if count % 100000 == 0:
                await log('processed %i intervals' % count)

            yield iv
    # Iterate through all intervals to construct the main index:
    intervalIndexes['main'] = IntervalTree([iv async for iv in intervalIterator()])

    # Store the domain of the data from the computed index as metadata
    self.datasets[label]['meta']['intervalDomain'] = [
        intervalIndexes['main'].top_node.begin,
        intervalIndexes['main'].top_node.end
    ]
    await log('')
    await log('Finished indexing %i intervals' % count)

    await log('Connecting intervals with the same GUID (.=2500 intervals)')
    intervalCount = missingCount = newLinks = seenLinks = 0
    for iv in intervalIndexes['main'].iterOverlap(endOrder=True):
        intervalId = iv.data
        intervalObj = intervals[intervalId]

        # Parent GUIDs refer to the one in the enter event, not the leave event
        guid = intervalObj.get('GUID', intervalObj['enter'].get('GUID', None))

        if guid is None:
            missingCount += 1
        else:
            if not guid in guids:
                guids[guid] = []
            guids[guid] = guids[guid] + [intervalId]

        # Connect to most recent interval with the parent GUID
        parentGuid = intervalObj.get('Parent GUID', intervalObj['enter'].get('Parent GUID', None))

        if parentGuid is not None and parentGuid in guids:
            foundPrior = False
            for parentIntervalId in reversed(guids[parentGuid]):
                parentInterval = intervals[parentIntervalId]
                if parentInterval['enter']['Timestamp'] <= intervalObj['enter']['Timestamp']:
                    foundPrior = True
                    intervalCount += 1
                    # Store metadata about the most recent interval
                    intervalObj['lastParentInterval'] = {
                        'id': parentIntervalId,
                        'location': parentInterval['Location'],
                        'endTimestamp': parentInterval['leave']['Timestamp']
                    }
                    # Because intervals is a diskcache, it needs a copy to know that something changed
                    intervals[intervalId] = intervalObj.copy()

                    # While we're here, note the parent-child link in the primitive graph
                    # (for now, only assume links from the parent's leave interval to the
                    # child's enter when primitive names are mismatched)
                    child = intervalObj.get('Primitive', intervalObj['enter'].get('Primitive', None))
                    parent = parentInterval.get('Primitive', intervalObj['leave'].get('Primitive', None))
                    if child is not None and parent is not None:
                        l = self.addPrimitiveChild(label, parent, child, 'otf2')[1]
                        newLinks += l
                        seenLinks += 1 if l == 0 else 0
                    break
            if not foundPrior:
                missingCount += 1
        else:
            missingCount += 1

        if (missingCount + intervalCount) % 2500 == 0:
            await log('.', end='')
        if (missingCount + intervalCount) % 100000 == 0:
            await log('processed %i intervals' % (missingCount + intervalCount))

    await log('Finished connecting intervals')
    await log('Interval links created: %i, Intervals without prior parent GUIDs: %i' % (intervalCount, missingCount))
    await log('New primitive links based on GUIDs: %d, Observed existing links: %d' % (newLinks, seenLinks))
