// In-process capture: exclusion refers to the Python engine, never a helper PID.
// Callbacks only copy into a bounded SPSC ring; Python drains it off the audio thread.
#import <Foundation/Foundation.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreAudio/CATapDescription.h>
#import <CoreAudio/AudioHardwareTapping.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreMedia/CoreMedia.h>
#include <stdatomic.h>
#include <unistd.h>

#define SLOTS 64
#define CAPACITY 65536
static unsigned char ring[SLOTS][CAPACITY];
static uint32_t lengths[SLOTS];
static _Atomic unsigned head, tail, dropped;
static double rate = 48000;
static unsigned channels = 2;
static char failure[1024];
static AudioObjectID tap, aggregate;
static AudioDeviceIOProcID io;
static SCStream *stream;
static id receiver;
static NSObject *lifecycle;
static _Atomic unsigned generation;

static void enqueue(const AudioBufferList *buffers) {
    if (!buffers || !buffers->mNumberBuffers) return;
    unsigned w = atomic_load_explicit(&head, memory_order_relaxed);
    unsigned next = (w + 1) % SLOTS;
    if (next == atomic_load_explicit(&tail, memory_order_acquire)) {
        atomic_fetch_add(&dropped, 1); return;
    }
    const AudioBuffer *b = &buffers->mBuffers[0];
    if (!b->mData || !b->mDataByteSize) return;
    unsigned size = b->mDataByteSize;
    if (buffers->mNumberBuffers == 1) {
        if (size > CAPACITY) { atomic_fetch_add(&dropped, 1); return; }
        memcpy(ring[w], b->mData, size);
    } else {
        // Core Audio/SCK may supply planar float32; preserve stereo interleaving.
        if (buffers->mNumberBuffers != channels || size * channels > CAPACITY) {
            atomic_fetch_add(&dropped, 1); return;
        }
        for (unsigned c = 0; c < channels; c++) {
            const AudioBuffer *plane = &buffers->mBuffers[c];
            if (!plane->mData || plane->mDataByteSize != size) return;
            for (unsigned f = 0; f < size / sizeof(float); f++)
                ((float *)ring[w])[f * channels + c] = ((float *)plane->mData)[f];
        }
        size *= channels;
    }
    lengths[w] = size;
    atomic_store_explicit(&head, next, memory_order_release);
}

@interface VBOutput : NSObject <SCStreamOutput, SCStreamDelegate>
@property(nonatomic) unsigned token;
@end
@implementation VBOutput
- (void)stream:(SCStream *)s didOutputSampleBuffer:(CMSampleBufferRef)sample
       ofType:(SCStreamOutputType)type {
    if (self.token != atomic_load(&generation)) return;
    if (type != SCStreamOutputTypeAudio || !CMSampleBufferDataIsReady(sample)) return;
    struct { AudioBufferList list; AudioBuffer extra; } storage;
    CMBlockBufferRef block = NULL;
    OSStatus err = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sample, NULL, &storage.list, sizeof(storage), NULL, NULL, 0, &block);
    if (!err) enqueue(&storage.list);
    if (block) CFRelease(block);
}
- (void)stream:(SCStream *)s didStopWithError:(NSError *)error {
    if (self.token != atomic_load(&generation)) return;
    snprintf(failure, sizeof(failure), "ScreenCaptureKit stopped: %s", error.localizedDescription.UTF8String);
}
@end

void vb_close(void) {
    atomic_fetch_add(&generation, 1);
    if (stream) { [stream stopCaptureWithCompletionHandler:^(NSError *e) { (void)e; }]; stream = nil; }
    receiver = nil;
    if (io) { AudioDeviceStop(aggregate, io); AudioDeviceDestroyIOProcID(aggregate, io); io = NULL; }
    if (aggregate) { AudioHardwareDestroyAggregateDevice(aggregate); aggregate = 0; }
    if (tap) { AudioHardwareDestroyProcessTap(tap); tap = 0; }
}

int vb_start(int backend) {
    @autoreleasepool {
        static dispatch_once_t once;
        dispatch_once(&once, ^{ lifecycle = [NSObject new]; });
        if (stream || tap) return -1;
        failure[0] = 0; head = tail = dropped = 0;
        rate = 48000; channels = 2;
        if (backend == 0) {
            pid_t pid = getpid(); AudioObjectID process = 0; UInt32 size = sizeof(process);
            AudioObjectPropertyAddress addr = {kAudioHardwarePropertyTranslatePIDToProcessObject,
                kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
            OSStatus err = AudioObjectGetPropertyData(kAudioObjectSystemObject, &addr,
                sizeof(pid), &pid, &size, &process);
            // Never capture without a verified self-exclusion entry.
            if (err || !process) {
                snprintf(failure, sizeof(failure), "Cannot resolve engine audio process (OSStatus %d)", err);
                return -1;
            }
            CATapDescription *desc = [[CATapDescription alloc]
                initStereoGlobalTapButExcludeProcesses:@[@(process)]];
            desc.name = @"Voztrad call audio"; desc.privateTap = YES;
            desc.muteBehavior = CATapUnmuted;
            NSDictionary *config = nil;
            err = AudioHardwareCreateProcessTap(desc, &tap);
            if (err) goto tap_error;
            AudioStreamBasicDescription fmt; size = sizeof(fmt);
            addr.mSelector = kAudioTapPropertyFormat;
            err = AudioObjectGetPropertyData(tap, &addr, 0, NULL, &size, &fmt);
            if (err) goto tap_error;
            if (fmt.mFormatID != kAudioFormatLinearPCM || !(fmt.mFormatFlags & kAudioFormatFlagIsFloat)
                || fmt.mBitsPerChannel != 32) { err = -1; goto tap_error; }
            rate = fmt.mSampleRate; channels = fmt.mChannelsPerFrame;
            config = @{
                @kAudioAggregateDeviceNameKey: @"Voztrad private capture",
                @kAudioAggregateDeviceUIDKey: NSUUID.UUID.UUIDString,
                @kAudioAggregateDeviceIsPrivateKey: @YES,
                @kAudioAggregateDeviceTapAutoStartKey: @YES,
                @kAudioAggregateDeviceTapListKey: @[@{
                    @kAudioSubTapUIDKey: desc.UUID.UUIDString,
                    @kAudioSubTapDriftCompensationKey: @YES}]
            };
            err = AudioHardwareCreateAggregateDevice((__bridge CFDictionaryRef)config, &aggregate);
            if (err) goto tap_error;
            err = AudioDeviceCreateIOProcIDWithBlock(&io, aggregate, NULL,
                ^(const AudioTimeStamp *now, const AudioBufferList *input,
                  const AudioTimeStamp *inputTime, AudioBufferList *output,
                  const AudioTimeStamp *outputTime) {
                    (void)now; (void)inputTime; (void)output; (void)outputTime;
                    enqueue(input);
                });
            if (err) goto tap_error;
            err = AudioDeviceStart(aggregate, io);
            if (err) goto tap_error;
            return 0;
        tap_error:
            snprintf(failure, sizeof(failure), "Core Audio tap failed (OSStatus %d). Check System Settings > Privacy & Security > Screen & System Audio Recording, then restart.", err);
            vb_close(); return -1;
        }
        // SCK runs IN THIS PROCESS, so excludesCurrentProcessAudio excludes our TTS.
        dispatch_semaphore_t ready = dispatch_semaphore_create(0);
        __block int result = -1;
        unsigned token = ++generation;
        [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES
            completionHandler:^(SCShareableContent *content, NSError *error) {
            @synchronized(lifecycle) {
            if (token != generation) { dispatch_semaphore_signal(ready); return; }
            if (error || !content.displays.count) {
                snprintf(failure, sizeof(failure), "ScreenCaptureKit: %s. Allow Screen & System Audio Recording and restart.",
                    error ? error.localizedDescription.UTF8String : "No display available");
                dispatch_semaphore_signal(ready); return;
            }
            SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:content.displays[0]
                excludingApplications:@[] exceptingWindows:@[]];
            SCStreamConfiguration *config = [SCStreamConfiguration new];
            config.capturesAudio = YES; config.excludesCurrentProcessAudio = YES;
            config.sampleRate = 48000; config.channelCount = 2;
            config.width = 2; config.height = 2; config.minimumFrameInterval = CMTimeMake(1, 1);
            receiver = [VBOutput new];
            [(VBOutput *)receiver setToken:token];
            stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:receiver];
            NSError *addError = nil;
            BOOL added = [stream addStreamOutput:receiver type:SCStreamOutputTypeAudio
                sampleHandlerQueue:dispatch_queue_create("voztrad.capture", DISPATCH_QUEUE_SERIAL)
                error:&addError];
            if (!added) {
                snprintf(failure, sizeof(failure), "ScreenCaptureKit output: %s", addError.localizedDescription.UTF8String);
                dispatch_semaphore_signal(ready); return;
            }
            [stream startCaptureWithCompletionHandler:^(NSError *startError) {
                @synchronized(lifecycle) {
                if (token != generation) { dispatch_semaphore_signal(ready); return; }
                if (startError) snprintf(failure, sizeof(failure), "ScreenCaptureKit start: %s", startError.localizedDescription.UTF8String);
                else result = 0;
                dispatch_semaphore_signal(ready);
                }
            }];
            }
        }];
        // Completion blocks own the semaphore, including when TCC is awaiting a user response.
        if (dispatch_semaphore_wait(ready, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC))) {
            @synchronized(lifecycle) {
                ++generation;
                snprintf(failure, sizeof(failure), "ScreenCaptureKit timed out. Grant Screen & System Audio Recording permission to the launcher, then restart. A graphical desktop session is required.");
                result = -1;
            }
        }
        if (result) vb_close();
        return result;
    }
}
int vb_read(void *destination, int capacity) {
    unsigned r = atomic_load_explicit(&tail, memory_order_relaxed);
    if (r == atomic_load_explicit(&head, memory_order_acquire)) return 0;
    unsigned n = lengths[r];
    if (capacity < 0 || n > (unsigned)capacity) return -1;
    memcpy(destination, ring[r], n);
    atomic_store_explicit(&tail, (r + 1) % SLOTS, memory_order_release);
    return n;
}
double vb_rate(void) { return rate; }
unsigned vb_channels(void) { return channels; }
unsigned vb_dropped(void) { return atomic_load(&dropped); }
const char *vb_error(void) { return failure; }
